"""Key-level reads of dict-valued attributes: which keys of a table does a test look up?

    python trace_keys.py OUT [--seed N] [--first A] [--last B] [--timeout S] [--budget S]
                             [--no-wrap] [--lines] [--only N,M,...]

Run inside the lab image. It builds on tools/trace_attrs.py: every plain data
attribute of every sqlglot class is a descriptor that notes the pair read. Here a
class attribute whose value is a plain dict gets, as the value the descriptor
hands out, a RecDict (tools/recdict.py, recdict.wrap): a dict with the same contents
that notes each key looked up ("key" notes) or, for anything that depends on more
than one key, the table ("whole" notes). Dicts in module namespaces with a
constant's name are handed out the same way (pair "module:|NAME", the form
selector/state.py reports module attributes in).

A table is replaced only when nothing else holds it (the holder check of
recdict.wrap; module namespaces are the other holders this tool moves). A table
that something else holds, or that is handed to exec or eval as a namespace,
stays a plain dict: its pairs are listed under "whole_tables" in pairs.json and the
builder treats every reader of such a pair as a whole reader (the pair level map).

Naming (selector/state.py key_names, the only naming there is): a lookup that hit
is named by the key the table holds (recdict.stored), a lookup that missed by the
key used; when both exist the names are united. No name (None): the table is read as a whole.

--lines: the child also records the sqlglot lines it executes (sys.monitoring,
as tools/trace trace does) and the digest goes into the row: the guard that the
stand-ins change nothing a test does. Run once with and once without --no-wrap
(same seed) and compare with tools/compare_guard.py.

Output, in OUT: pairs.json and results.jsonl, one line per test:
  {"n", "id", "ok", "pairs": [numbers of the pairs read, class pairs by their descriptor],
   "keys": {pair number: [names]}, "whole": [pair numbers read as a whole table],
   "bypass": [...], "cpu", "wall", ("lines": digest, "nlines")}
A pair that was read (its descriptor ran) with no note from its table is a whole read.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import sys
import types
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import recdict  # noqa: E402
import trace_attrs as ta  # noqa: E402  (imports unittest before sqlglot)

if os.environ.get("BR_ROOT"):
    ta.ROOT = os.environ["BR_ROOT"]  # a local smoke test outside the lab image

_CONSTANT = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
# Tables that reach exec or eval as a namespace: lookups there bypass any override. Filled by find_exec_tables().
EXEC_TABLES: set = set()


def _module_pair(module_name: str, name: str) -> str:
    return f"{module_name}:|{name}"


def find_exec_tables() -> None:
    """Fill EXEC_TABLES: names of tables that the package hands to exec or eval as a namespace."""
    import ast

    base = os.path.join(ta.ROOT, ta.PACKAGE)
    for directory, _subdirs, files in os.walk(base):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(directory, name)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval"):
                    for argument in node.args[1:3]:
                        if isinstance(argument, ast.Name):
                            EXEC_TABLES.add(argument.id)
                        elif isinstance(argument, ast.Attribute):
                            EXEC_TABLES.add(argument.attr)


def _namespaces() -> dict:
    found = {}
    for name, module in list(sys.modules.items()):
        if module is not None and (name == ta.PACKAGE or name.startswith(ta.PACKAGE + ".")):
            found[id(vars(module))] = (name, vars(module))
    return found


def _module_groups(namespaces: dict) -> dict:
    groups: dict = {}
    for _id, (module_name, namespace) in namespaces.items():
        for attribute in list(namespace):
            value = namespace[attribute]
            if type(value) is dict and _CONSTANT.match(attribute):
                group = groups.setdefault(id(value), {"value": value, "modules": []})
                group["modules"].append((module_name, attribute))
            del value
    return groups


def _holder_reason(group: dict, namespaces: dict, mine: set):
    value = group["value"]
    here = (os.path.abspath(__file__), recdict.__file__)
    for holder in gc.get_referrers(value):
        if id(holder) in mine or holder is group:
            continue
        if isinstance(holder, types.FrameType) and holder.f_code.co_filename in here:
            continue
        if type(holder) is dict and id(holder) in namespaces:
            if any(v is value and not _CONSTANT.match(k) for k, v in holder.items()):
                return "also held in a module under a name that is not a constant's"
            continue
        return f"also held by a {type(holder).__name__}"
    return None


def wrap_module_tables(watched: dict) -> tuple:
    """Dicts held by a module namespace under a constant's name (the class descriptors that hold the same
    object included). Returns (key pairs, {pair: reason} for the ones left alone)."""
    namespaces = _namespaces()
    groups = _module_groups(namespaces)
    key_pairs: list = []
    whole: dict = {}
    for group in list(groups.values()):
        value = group["value"]
        descriptors = [(pair, d) for pair, d in watched.items() if object.__getattribute__(d, "_br_value") is value]
        pairs = [pair for pair, _ in descriptors] + [_module_pair(*entry) for entry in group["modules"]]
        reason = _holder_reason(group, namespaces, {id(d) for _, d in descriptors})
        if reason is None and any(attribute in EXEC_TABLES for _, attribute in group["modules"]):
            reason = "used as the namespace of exec or eval"
        if reason is None and any(pair.rsplit("|", 1)[1] in EXEC_TABLES for pair, _ in descriptors):
            reason = "used as the namespace of exec or eval"
        if reason is not None:
            whole.update({pair: reason for pair in pairs})
            continue
        table = recdict.RecDict(value, pairs)
        for _, descriptor in descriptors:
            ta._Logged._br_value.__set__(descriptor, table)
        for _id, (_name, namespace) in namespaces.items():
            for attribute in list(namespace):
                if namespace[attribute] is value:
                    namespace[attribute] = table
        key_pairs.extend(pairs)
    return key_pairs, whole


def wrap_attributes() -> dict:
    """ta.wrap_attributes(), then dict-valued attributes replaced by RecDicts where that is faithful.

    Returns the dict ta returns, with "pairs" (every pair that notes reads, module pairs of replaced
    tables included), "key_pairs" (those whose keys are noted) and "whole_tables" ({pair: reason}).
    """
    info = ta.wrap_attributes()
    watched = {}
    for pair in info["pairs"]:
        module_name, rest = pair.split(":", 1)
        qualname, attribute = rest.rsplit("|", 1)
        cls = ta._resolve(module_name, qualname)
        descriptor = vars(cls).get(attribute) if cls is not None else None
        if isinstance(descriptor, ta._Logged):
            watched[pair] = descriptor
    key_pairs, whole = wrap_module_tables(watched)
    rest_pairs = {pair: d for pair, d in watched.items() if pair not in key_pairs and pair not in whole}
    more, left = recdict.wrap(rest_pairs, slot="_br_value", ignore=[watched])
    key_pairs.extend(more)
    whole.update(left)
    for pair, descriptor in watched.items():
        # dict-valued pairs that are not plain dicts (OrderedDict, defaultdict, a subclass) are whole as well
        if pair not in key_pairs and pair not in whole:
            value = object.__getattribute__(descriptor, "_br_value")
            if isinstance(value, dict):
                whole[pair] = f"a {type(value).__name__}, not a plain dict"
    info["pairs"] = sorted(set(info["pairs"]) | {pair for pair in key_pairs if ":|" in pair} | {pair for pair in whole if ":|" in pair})
    info["key_pairs"] = sorted(key_pairs)
    info["whole_tables"] = sorted([pair, reason] for pair, reason in whole.items())
    return info


# ---- turning the notes of a run into names ------------------------------------


def snapshot(number_of: dict) -> dict:
    """What the notes of this process mean: {"keys": {pair number: [names]}, "whole": [pair numbers]}."""
    from selector import state

    names: dict = {}
    whole: set = set()
    touched: set = set()
    for table_id, key in list(recdict.KEYS):
        table = recdict.TABLES.get(table_id)
        if table is None:
            continue
        touched.add(table_id)
        try:
            held = recdict.stored(table, key)
            found = state.key_names(held)
            if found is not None and held is not key:
                used = state.key_names(key)
                if used is not None:
                    found = found | used
        except Exception:  # noqa: BLE001 - what cannot be named is read whole
            found = None
        for pair in table._br_pairs:
            if pair in number_of:
                if found is None:
                    whole.add(number_of[pair])
                else:
                    names.setdefault(number_of[pair], set()).update(found)
    for table_id in list(recdict.WHOLE):
        table = recdict.TABLES.get(table_id)
        if table is not None:
            touched.add(table_id)
            whole.update(number_of[pair] for pair in table._br_pairs if pair in number_of)
    return {
        "keys": {str(number): sorted(found) for number, found in sorted(names.items()) if number not in whole},
        "whole": sorted(whole),
        "touched": sorted({number_of[pair] for table_id in touched for pair in recdict.TABLES[table_id]._br_pairs if pair in number_of}),
    }


def reset() -> None:
    recdict.KEYS.clear()
    recdict.WHOLE.clear()


# ---- the guard: executed lines -------------------------------------------------

LINES: set = set()
_TOOL = None


def start_lines() -> None:
    global _TOOL
    mon = sys.monitoring
    _TOOL = mon.COVERAGE_ID
    prefix = ta.ROOT + "/" + ta.PACKAGE + "/"
    disable = mon.DISABLE
    seen = LINES

    def on_line(code, line):
        if code.co_filename.startswith(prefix):
            seen.add((code.co_filename, code.co_firstlineno, line))
        return disable

    def on_start(code, offset):
        if code.co_filename.startswith(prefix):
            seen.add((code.co_filename, code.co_firstlineno, 0))
        return disable

    mon.use_tool_id(_TOOL, "keys-guard")
    mon.register_callback(_TOOL, mon.events.LINE, on_line)
    mon.register_callback(_TOOL, mon.events.PY_START, on_start)
    mon.set_events(_TOOL, mon.events.LINE | mon.events.PY_START)


def stop_lines() -> list:
    sys.monitoring.set_events(_TOOL, 0)
    return sorted(LINES)


# ---- the full-suite tracer: one fork per test ---------------------------------


def run_in_child(write_fd: int, number: int, test_id: str, number_of: dict, lines_dir) -> None:
    try:
        os.setpgid(0, 0)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        from selector import runner

        ta.READS.clear()
        reset()
        if lines_dir:
            start_lines()
        messages: list = []
        runner.run_tests([(number, test_id)], messages.append)
        finished = [line.split() for line in messages if line.startswith("R ")]
        payload = {
            "ok": bool(finished) and all(line[2] == "0" for line in finished),
            "pairs": sorted(number_of[pair] for pair in ta.READS if pair in number_of),
            "bypass": sorted([number_of[pair], how, where] for pair, how, where in ta.BYPASS if pair in number_of),
        }
        if lines_dir:
            lines = stop_lines()
            text = "\n".join(f"{name[len(ta.ROOT) + 1:]}:{first}:{line}" for name, first, line in lines).encode()
            payload["lines"] = hashlib.sha1(text).hexdigest()[:16]
            payload["nlines"] = len(lines)
            with open(os.path.join(lines_dir, f"{number}.z"), "wb") as handle:
                handle.write(zlib.compress(text, 6))
        payload.update(snapshot(number_of))
        payload["cpu"] = round(sum(os.times()[:2]), 2)
        data = json.dumps(payload, separators=(",", ":")).encode()
        view = memoryview(data)
        while view:
            view = view[os.write(write_fd, view):]
    except BaseException as error:  # noqa: BLE001
        try:
            os.write(write_fd, json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"[:300]}).encode())
        except OSError:
            pass
    finally:
        os._exit(0)


def run_test(timeout: float, *args) -> dict:
    """ta.run_test with this module's child."""
    import select
    import signal
    import time

    read_fd, write_fd = os.pipe()
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        run_in_child(write_fd, *args)
        os._exit(1)
    os.close(write_fd)
    chunks: list = []
    timed_out = False
    while True:
        left = timeout - (time.monotonic() - started)
        if left <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([read_fd], [], [], min(left, 5.0))
        if ready:
            chunk = os.read(read_fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    os.close(read_fd)
    if timed_out:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    _, status = os.waitpid(pid, 0)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        result = json.loads(b"".join(chunks))
    except ValueError:
        result = {"ok": False, "error": "timeout" if timed_out else f"died, status {status}"}
    result["wall"] = round(time.monotonic() - started, 2)
    return result


def main() -> None:
    import time

    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--first", type=int, default=0)
    parser.add_argument("--last", type=int, default=1 << 30)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--budget", type=float, default=1500.0, help="stop starting tests after this many seconds (exit 3: run again)")
    parser.add_argument("--no-wrap", action="store_true", help="the guard's baseline: nothing replaced at all")
    parser.add_argument("--no-tables", action="store_true", help="descriptors only, no RecDict")
    parser.add_argument("--lines", action="store_true", help="record the executed sqlglot lines of each test (the guard)")
    parser.add_argument("--only", default="")
    arguments = parser.parse_args()
    arguments.out = os.path.abspath(arguments.out)
    if os.environ.get("PYTHONHASHSEED") != str(arguments.seed):
        os.environ["PYTHONHASHSEED"] = str(arguments.seed)
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    started = time.monotonic()
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path[:] = [ta.ROOT, os.path.dirname(HERE)] + [entry for entry in sys.path if entry not in ("", HERE)]
    sys.path.insert(1, HERE)
    os.chdir(ta.ROOT)
    from selector import runner

    runner._cap_duckdb_threads()
    ids = json.load(open(os.path.join(os.path.dirname(HERE), "maps", "tests.json")))
    ta.preload()
    find_exec_tables()
    if arguments.no_wrap:
        info = {"pairs": [], "key_pairs": [], "whole_tables": []}
    elif arguments.no_tables:
        info = ta.wrap_attributes()
        info.update({"key_pairs": [], "whole_tables": []})
    else:
        info = wrap_attributes()
    # Setting the stand-ins up touched them (hasattr on a descriptor is a "bypass"): not a test's reads.
    ta.READS.clear()
    ta.BYPASS.clear()
    os.makedirs(arguments.out, exist_ok=True)
    lines_dir = None
    if arguments.lines:
        lines_dir = os.path.join(arguments.out, "lines")
        os.makedirs(lines_dir, exist_ok=True)
    with open(os.path.join(arguments.out, "pairs.json"), "w") as handle:
        json.dump(info, handle, separators=(",", ":"))
    number_of = {pair: number for number, pair in enumerate(info["pairs"])}
    print({key: len(value) if isinstance(value, list) else value for key, value in info.items() if key not in ("pairs", "unreachable_names")}, len(info["pairs"]), "pairs", flush=True)
    results_path = os.path.join(arguments.out, "results.jsonl")
    done = set()
    if os.path.exists(results_path):
        done = {json.loads(line)["n"] for line in open(results_path) if line.endswith("\n")}
    numbers = range(arguments.first, min(arguments.last, len(ids)))
    if arguments.only:
        numbers = [int(part) for part in arguments.only.split(",") if part]
    with open(results_path, "a") as log:
        for number in numbers:
            if number in done:
                continue
            if time.monotonic() - started > arguments.budget:
                print("budget used up at", number, flush=True)
                sys.exit(3)
            result = run_test(arguments.timeout, number, ids[number], number_of, lines_dir)
            result.update({"n": number, "id": ids[number]})
            log.write(json.dumps(result, separators=(",", ":")) + "\n")
            log.flush()
            if not result["ok"]:
                print("NOT OK", number, ids[number], result.get("error", ""), flush=True)
    print("complete", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
