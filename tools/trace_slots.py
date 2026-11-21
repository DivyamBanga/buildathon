"""Which code reads the slots _type, _meta, comments and _hash of the nodes of an optimized TPC-DS query?

    python trace_slots.py --seed 0 [--plain] [--out DIR] [--only 3,5]   (one fork per query, one result file per query)
    python trace_slots.py --report DIR [DIR ...]                         (table and slots.json from result files)
    python trace_slots.py --selftest

The executor memo (selector/runner.py: install_executor_memo) keys a recorded
result by a digest of the optimized tree. The digest covers four slots of
every node: _type (type annotation), _meta (token positions), comments and
_hash. This tool finds which of them the planner and the executor really read.

It runs like tools/build_executor_memo.py: the parent imports the pinned
package and tests/test_executor.py and runs the class setup once; for every
executed TPC-DS case a fork runs that query alone, for real (no recorded result
is used). In the fork the member descriptors Expression._type, ._meta,
.comments and ._hash are replaced by Python descriptors that delegate to the
originals and note the reading frame (file, first line of the code object,
current line, function name) while a window is open.

The window opens when plan() hands the optimized tree to runner.tree_digests
(after the digest was computed: the digest reads every slot itself) and closes
at the next phase mark "L" (end of the executor's execute). Everything before
it (ensure_tables, optimize) is outside. Inside it every read and write is
split by whose slot it is: "tree" (a node reachable from the root handed to
Plan, type annotations included), "copy" (a node made by Expr.__deepcopy__ from
a tree or copy node, found by walking source and result side by side),
"build" (a node that a __deepcopy__ of a tree or copy node is still
constructing, touched by the construction itself: Expr.set and append clear
its _hash, __init__ sets all four) and "other" (made by the planner or the
generator).
"""
import argparse
import hashlib
import json
import linecache
import os
import pickle
import shutil
import sys
import tempfile
import zlib

SLOTS = ("_type", "_meta", "comments", "_hash")
SQLGLOT = "/sqlglot/"
FIXTURE = "optimizer/tpc-ds/tpc-ds.sql"


class S:
    """State of the tracer in this process."""

    is_open = False
    busy = False  # the tracer's own walks: not counted
    tree: dict = {}
    copy: dict = {}
    reads: dict = {}  # (slot, origin, frame) -> count
    writes: dict = {}
    via: dict = {}  # (slot, origin, frame) -> {caller frame: count}
    windows = 0
    mismatch = 0
    copying = 0  # depth of Expr.__deepcopy__ calls whose source is a tree or copy node
    key = None
    Expr = None


def frame_of(f):
    code = f.f_code
    name = code.co_filename
    if name.startswith(SQLGLOT):
        name = name[len(SQLGLOT):]
    return (name, code.co_firstlineno, f.f_lineno, code.co_name)


def note(kind, slot, inst, f):
    i = id(inst)
    origin = "tree" if i in S.tree else "copy" if i in S.copy else "build" if S.copying else "other"
    fr = frame_of(f)
    k = (slot, origin, fr)
    table = S.reads if kind == "get" else S.writes
    table[k] = table.get(k, 0) + 1
    if kind == "get" and origin != "other" and f.f_back is not None:
        callers = S.via.setdefault(k, {})
        c = frame_of(f.f_back)
        if c in callers or len(callers) < 30:
            callers[c] = callers.get(c, 0) + 1


class Slot:
    """Stands in for a member descriptor of __slots__: delegates, and notes while a window is open."""

    __slots__ = ("name", "orig")

    def __init__(self, name, orig):
        self.name = name
        self.orig = orig

    def __get__(self, inst, owner=None):
        if inst is not None and S.is_open and not S.busy:
            note("get", self.name, inst, sys._getframe(1))
        return self.orig.__get__(inst, owner)

    def __set__(self, inst, value):
        if S.is_open and not S.busy:
            note("set", self.name, inst, sys._getframe(1))
        self.orig.__set__(inst, value)

    def __delete__(self, inst):
        if S.is_open and not S.busy:
            note("set", self.name, inst, sys._getframe(1))
        self.orig.__delete__(inst)


def all_classes(Expr):
    seen, pending = [], [Expr]
    ids = set()
    while pending:
        cls = pending.pop()
        if id(cls) in ids:
            continue
        ids.add(id(cls))
        seen.append(cls)
        pending.extend(cls.__subclasses__())
    return seen


def install_descriptors(Expr) -> dict:
    """Replace every member descriptor of the four slots on every class in the tree of Expr; returns a summary."""
    import types

    patched, overrides = [], []
    for cls in all_classes(Expr):
        for slot in SLOTS:
            value = cls.__dict__.get(slot)
            if isinstance(value, types.MemberDescriptorType):
                setattr(cls, slot, Slot(slot, value))
                patched.append(f"{cls.__qualname__}.{slot}")
        if "__deepcopy__" in cls.__dict__:
            overrides.append(cls)
    return {"patched": patched, "deepcopy_defined_in": [c.__qualname__ for c in overrides]}


def uninstrumented(Expr) -> list:
    """Member descriptors of the four slots that appeared after installation (a class made later)."""
    import types

    return [
        f"{cls.__qualname__}.{slot}"
        for cls in all_classes(Expr)
        for slot in SLOTS
        if isinstance(cls.__dict__.get(slot), types.MemberDescriptorType)
    ]


def collect(root, into: dict) -> None:
    """Every node reachable from root through args and type annotations (recursively)."""
    Expr = S.Expr
    S.busy = True
    try:
        stack = [root]
        while stack:
            node = stack.pop()
            if id(node) in into:
                continue
            into[id(node)] = node
            for value in node.args.values():
                pending = [value]
                while pending:
                    item = pending.pop()
                    if isinstance(item, Expr):
                        stack.append(item)
                    elif isinstance(item, (list, tuple)):
                        pending.extend(item)
                    elif isinstance(item, dict):
                        pending.extend(item.values())
            annotation = getattr(node, "_type", None)
            if isinstance(annotation, Expr):
                stack.append(annotation)
    finally:
        S.busy = False


def open_window(root) -> None:
    S.tree = {}
    S.copy = {}
    collect(root, S.tree)
    S.is_open = True
    S.windows += 1


def close_window() -> None:
    S.is_open = False


def track_copy(src, dst) -> None:
    Expr = S.Expr
    stack = [(src, dst)]
    seen = set()
    while stack:
        s, d = stack.pop()
        if (id(s), id(d)) in seen:
            continue
        seen.add((id(s), id(d)))
        if id(s) in S.tree or id(s) in S.copy:
            if id(d) not in S.tree and id(d) not in S.copy:
                S.copy[id(d)] = d
        for k, sv in s.args.items():
            dv = d.args.get(k)
            if isinstance(sv, Expr):
                if isinstance(dv, Expr):
                    stack.append((sv, dv))
                else:
                    S.mismatch += 1
            elif type(sv) is list:
                if type(dv) is list and len(dv) == len(sv):
                    for a, b in zip(sv, dv):
                        if isinstance(a, Expr) and isinstance(b, Expr):
                            stack.append((a, b))
                        elif isinstance(a, Expr):
                            S.mismatch += 1
                else:
                    S.mismatch += 1


def install_copy_tracking(Expr) -> None:
    for cls in all_classes(Expr):
        original = cls.__dict__.get("__deepcopy__")
        if original is None or not callable(original):
            continue

        def make(original):
            def __deepcopy__(self, memo):
                derived = S.is_open and (id(self) in S.tree or id(self) in S.copy)
                if derived:
                    S.copying += 1
                try:
                    result = original(self, memo)
                finally:
                    if derived:
                        S.copying -= 1
                if S.is_open and not S.busy:
                    S.busy = True
                    try:
                        track_copy(self, result)
                    finally:
                        S.busy = False
                return result

            return __deepcopy__

        setattr(cls, "__deepcopy__", make(original))


def snapshot() -> dict:
    def rows(table):
        out = {slot: [] for slot in SLOTS}
        for (slot, origin, fr), count in sorted(table.items()):
            out[slot].append([origin, *fr, count])
        return out

    via = {}
    for (slot, origin, fr), callers in S.via.items():
        via.setdefault(slot, []).append([origin, *fr, [[*c, n] for c, n in sorted(callers.items())]])
    return {
        "reads": rows(S.reads),
        "writes": rows(S.writes),
        "via": via,
        "windows": S.windows,
        "tree_nodes": len(S.tree),
        "copy_nodes": len(S.copy),
        "copy_mismatch": S.mismatch,
    }


# ---- selftest ----------------------------------------------------------------

def selftest() -> None:
    sys.path.insert(0, "/sqlglot")
    from sqlglot import exp, parse_one

    S.Expr = exp.Expr
    info = install_descriptors(exp.Expr)
    install_copy_tracking(exp.Expr)
    print("patched:", info["patched"], "deepcopy defined in:", info["deepcopy_defined_in"])
    root = parse_one("SELECT a /* c1 */, b FROM t")
    root.expressions[0].type = "int"  # before the window: not counted
    S.reads, S.writes, S.via = {}, {}, {}
    open_window(root)
    column = root.expressions[0]
    column.type  # property: frame `type` in core.py
    root.expressions[1].meta  # property meta (reads _meta, writes it)
    column.comments  # direct read in this driver
    hash(root)  # _hash through __hash__
    clone = root.copy()  # reads in __deepcopy__ (copying along)
    clone.expressions[1].type  # a read of a node made by __deepcopy__ from the tree
    exp.column("z").type  # a node of nobody's tree
    column._type = None  # a write to a tree node
    close_window()
    column.type  # window closed: not counted
    snap = snapshot()
    for slot in SLOTS:
        for row in snap["reads"][slot]:
            print("GET", slot, row)
        for row in snap["writes"][slot]:
            print("SET", slot, row)
    print("tree nodes", snap["tree_nodes"], "copy nodes", snap["copy_nodes"], "mismatch", snap["copy_mismatch"])
    print("uninstrumented:", uninstrumented(exp.Expr))
    need = {
        ("_type", "tree", "type"),
        ("_meta", "tree", "meta"),
        ("comments", "tree", "selftest"),
        ("_hash", "tree", "__hash__"),
        ("_type", "copy", "type"),
        ("_type", "other", "type"),
        ("_type", "tree", "__deepcopy__"),
    }
    got = {(slot, row[0], row[4]) for slot in SLOTS for row in snap["reads"][slot]}
    missing = need - got
    print("selftest:", "FAILED, missing %s" % sorted(missing) if missing else "ok")


# ---- the real run ------------------------------------------------------------

def find_repo() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.environ.get("BR_REPO"), here, os.path.dirname(here), "/repo"):
        if candidate and os.path.exists(os.path.join(candidate, "selector", "runner.py")):
            return candidate
    raise SystemExit("no selector/runner.py found; set BR_REPO")


def main_run(args) -> None:
    seed = str(args.seed)
    if os.environ.get("PYTHONHASHSEED") != seed:
        os.execve(sys.executable, [sys.executable] + sys.argv, dict(os.environ, PYTHONHASHSEED=seed))
    repo = find_repo()
    sys.path.insert(0, repo)
    from selector import runner, state  # noqa: E402

    out_dir = os.path.join(args.out, ("plain-" if args.plain else "seed-") + seed)
    os.makedirs(out_dir, exist_ok=True)
    os.environ["SKIP_INTEGRATION"] = "0"
    runner.warm_up("/sqlglot", ["tests/test_executor.py"])
    import importlib

    for name in state.package_modules("/sqlglot"):
        try:
            importlib.import_module(name)
        except BaseException:  # noqa: BLE001
            pass
    from sqlglot import exp

    S.Expr = exp.Expr
    assert runner.EXECUTOR_MEMO is None, "the recorded results must not be loaded"
    runner.build_executor_tables()

    def phase(kind):
        if kind == "L" and S.is_open:
            close_window()

    runner.MEMO_PHASE = phase
    hook_name = "tree_digests" if hasattr(runner, "tree_digests") else "tree_digest"
    if not args.plain:
        real_digests = getattr(runner, hook_name)

        def hooked(root):
            collect_first = root
            result = real_digests(collect_first)  # reads every slot itself: before the window
            open_window(root)
            return result

        setattr(runner, hook_name, hooked)
    runner.install_executor_memo()
    runner._use_executor_tables()
    info = {"hook": hook_name}
    if not args.plain:
        info.update(install_descriptors(exp.Expr))
        install_copy_tracking(exp.Expr)
    print(json.dumps({k: (v if k == "hook" else len(v)) for k, v in info.items()}), flush=True)

    memo_file = os.path.join(repo, "maps", "execmemo.bin")
    recorded = {}
    if os.path.exists(memo_file):
        with open(memo_file, "rb") as handle:
            recorded = pickle.loads(zlib.decompress(handle.read()))

    helpers = sys.modules["tests.helpers"]
    count = len(list(helpers.load_sql_fixture_pairs(FIXTURE)))
    only = {int(x) for x in args.only.split(",")} if args.only else None
    real_key = runner.memo_key

    def noting(*a, **kw):
        key = real_key(*a, **kw)
        S.key = key
        keys.append(key)
        return key

    for index in range(count):
        path = os.path.join(out_dir, f"{index:03d}.json")
        if os.path.exists(path) or (only is not None and index not in only):
            continue
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                keys = []
                runner.memo_key = noting
                record = tempfile.mkdtemp(prefix="slots-")
                runner.MEMO_RECORD = record
                S.reads, S.writes, S.via, S.windows, S.mismatch = {}, {}, {}, 0, 0
                runner.CASE_FILTER[("pairs", FIXTURE)] = frozenset([index])
                ids = ["tests/test_executor.py::TestExecutor::test_execute_tpcds"]
                messages = []
                runner.run_tests(list(enumerate(ids)), messages.append)
                failed = [m for m in messages if m.startswith("C") or (m.startswith("R") and m.endswith(" 1\n"))]
                result = {"index": index, "keys": keys, "failed": failed}
                if len(keys) == 1:
                    result["key"] = keys[0]
                    for name in os.listdir(record):
                        with open(os.path.join(record, name), "rb") as handle:
                            key, typed, data = pickle.load(handle)
                        result["result_sha"] = hashlib.sha1(data).hexdigest()
                        result["typed_sha"] = hashlib.sha1(repr(typed).encode()).hexdigest()
                        if key in recorded:
                            one, two = pickle.loads(recorded[key]), pickle.loads(data)
                            result["same_as_recorded"] = recorded[key] == data or (
                                one.columns == two.columns and one.rows == two.rows
                            )
                    if not args.plain:
                        result.update(snapshot())
                        result["uninstrumented"] = uninstrumented(exp.Expr)
                shutil.rmtree(record, ignore_errors=True)
                with open(path + ".tmp", "w") as handle:
                    json.dump(result, handle)
                os.replace(path + ".tmp", path)
                print(index, "keys", len(keys), "failed", len(failed), "windows", S.windows if not args.plain else "-", flush=True)
            except BaseException as error:  # noqa: BLE001
                print(f"case {index} raised {error!r}", flush=True)
                code = 1
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        if status != 0:
            print(f"case {index}: child status {status}", flush=True)


# ---- report ------------------------------------------------------------------

def load_dir(directory) -> dict:
    out = {}
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json"):
            with open(os.path.join(directory, name)) as handle:
                entry = json.load(handle)
            if entry.get("key"):
                out[entry["key"]] = entry
    return out


def decisive(entry, slot):
    """Frames [path, first, line, name] that read a slot of a tree or copy node outside __deepcopy__."""
    return sorted(
        {tuple(r[1:5]) for r in entry["reads"][slot] if r[0] in ("tree", "copy") and r[4] != "__deepcopy__"}
    )


def src(path, line):
    return linecache.getline(SQLGLOT + path, line).strip()


def report(dirs, out_json) -> None:
    sets = {d: load_dir(d) for d in dirs}
    primary_dir = dirs[0]
    primary = sets[primary_dir]
    for d, entries in sets.items():
        bad = [e["key"] for e in entries.values() if e["failed"] or e.get("same_as_recorded") is False]
        print(f"{d}: {len(entries)} queries, failures or differing results: {bad}")
        print(f"  windows per query: {sorted({e.get('windows') for e in entries.values()})}, "
              f"copy mismatches: {sum(e.get('copy_mismatch', 0) for e in entries.values())}, "
              f"uninstrumented: {sorted({u for e in entries.values() for u in e.get('uninstrumented', [])})}")
    keys = sorted(primary)
    # slots.json: decisive frames of the primary run
    result = {key: {slot: [list(f[:3]) for f in decisive(primary[key], slot)] for slot in SLOTS} for key in keys}
    with open(out_json, "w") as handle:
        json.dump(result, handle, indent=1, sort_keys=True)
    with open(out_json.replace(".json", "_full.json"), "w") as handle:
        json.dump({d: entries for d, entries in sets.items()}, handle)
    print(f"\nrun {primary_dir}: {len(keys)} queries\n")
    for slot in SLOTS:
        frames = {}  # (path, first, line, name) -> origin -> number of queries
        for key in keys:
            seen = {}
            for origin, path, first, line, name, count in primary[key]["reads"][slot]:
                seen[(path, first, line, name, origin)] = True
            for (path, first, line, name, origin) in seen:
                frames.setdefault((path, first, line, name), {}).setdefault(origin, set()).add(key)
        print(f"== {slot}: reads of a slot inside the window (number of queries, by whose node)")
        for fr in sorted(frames, key=lambda f: (-len(frames[f].get("tree", set()) | frames[f].get("copy", set())), f)):
            by = frames[fr]
            counts = " ".join(f"{o}={len(by.get(o, ()))}" for o in ("tree", "copy", "build", "other"))
            tag = "copying along (does not count)" if fr[3] == "__deepcopy__" else "READ"
            print(f"  {fr[0]}:{fr[2]} in {fr[3]} (from line {fr[1]})  {counts}  [{tag}]")
            print(f"      {src(fr[0], fr[2])}")
        n = sum(1 for key in keys if decisive(primary[key], slot))
        print(f"  queries with a read of a tree or copy node outside __deepcopy__: {n} of {len(keys)}")
        callers = {}
        for key in keys:
            for origin, path, first, line, name, cs in primary[key]["via"].get(slot, []):
                if name != "__deepcopy__":
                    for c in cs:
                        callers.setdefault((path, line, name), {}).setdefault((c[0], c[2], c[3]), set()).add(key)
        for fr, cs in sorted(callers.items()):
            print(f"  callers of {fr[2]} ({fr[0]}:{fr[1]}):")
            for c, qs in sorted(cs.items(), key=lambda kv: -len(kv[1]))[:12]:
                print(f"      {c[0]}:{c[1]} in {c[2]}  queries={len(qs)}")
        print()
    print("== writes inside the window")
    writes = {}
    for key in keys:
        for slot in SLOTS:
            for origin, path, first, line, name, count in primary[key]["writes"][slot]:
                writes.setdefault((slot, path, line, name, origin), set()).add(key)
    for (slot, path, line, name, origin), qs in sorted(writes.items()):
        print(f"  SET {slot} of {origin} node: {path}:{line} in {name}  queries={len(qs)}   {src(path, line)}")
    if not writes:
        print("  none")
    if len(dirs) > 1:
        print("\n== differences between runs (decisive frames per query and slot)")
        diff = 0
        for key in keys:
            for slot in SLOTS:
                versions = [tuple(decisive(sets[d][key], slot)) if key in sets[d] else None for d in dirs]
                if len(set(versions)) > 1:
                    diff += 1
                    print(f"  {key[-30:]} {slot}: {versions}")
        print(f"  {diff} differences")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
    parser.add_argument("--only")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--report", nargs="+")
    parser.add_argument("--json", default="slots.json")
    args = parser.parse_args()
    if args.selftest:
        selftest()
    elif args.report:
        report(args.report, args.json)
    else:
        main_run(args)


if __name__ == "__main__":
    main()
