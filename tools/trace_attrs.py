"""Record which tests read which class attributes of sqlglot (input of maps/areaders.bin).

    python trace_attrs.py OUT [--seed N] [--first A] [--last B] [--timeout S] [--no-wrap]
                              [--warm I,J,...]

Run inside the lab image (pinned sqlglot at /sqlglot, selector/ next to this
tool's parent directory). sqlglot is imported in full, as the split tracer
(trace/sitecustomize.py with BR_TRACE_SPLIT) does before a test starts. Then
every data attribute found in the __dict__ of a class of sqlglot is replaced by
a non-data descriptor that notes (class, attribute) when it is read, through
the class or through an instance; the value it returns is the one it replaced.
Each test then runs alone in a fork of that process, the way the selector's
children run them (runner.run_tests: the test module is imported in the child,
class setup included, process pools are replaced by the in-process stand-in).

Which attributes are wrapped: those in the own __dict__ of a class that its
module can be reached from by name (state.py reports differences against such
a class: the pair is "module:Qualname|attribute"), except names with two
leading and trailing underscores, state Python keeps for itself, and anything
else state.watchable refuses (callables, anything whose type defines __get__:
functions, properties, class and static methods; everything in an Enum), and
anything whose type defines __set__ or __delete__.

A descriptor that is read out of a class dict (vars(cls), cls.__dict__) and
used from there is a "bypass": the object stands in for its value (see
_Logged) and every use is noted as a read and listed with the innermost
sqlglot or tests frame in the output.

Output, in OUT:
  pairs.json     every pair wrapped
  results.jsonl  one line per test: number, id, ok, pairs read, bypass events,
                 (--warm: numbers of tests run first in the same process)
A test that does not pass under tracing, dies or runs out of time is "not ok":
tools/build_areaders.py makes it a candidate for every pair.
"""

from __future__ import annotations

import argparse
import enum
import importlib
import json
import os
import select
import signal
import sys
import time
import unittest  # noqa: F401 - sqlglot checks, when imported, whether tests are loaded

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = "/sqlglot"
PACKAGE = "sqlglot"

READS: set = set()
BYPASS: set = set()

# Names Python and the abc module keep for themselves (state._CLASS_NOISE).
_NOISE = frozenset(("_abc_impl",))


def _bypass(logged, kind: str) -> None:
    pair = logged._br_pair
    READS.add(pair)
    frame = sys._getframe(2)
    while frame is not None and not frame.f_code.co_filename.startswith(ROOT + "/"):
        frame = frame.f_back
    where = f"{frame.f_code.co_filename[len(ROOT) + 1:]}:{frame.f_lineno}" if frame is not None else "?"
    BYPASS.add((pair, kind, where))


class _Logged:
    """Replaces a class attribute. Read as an attribute, it notes the pair and gives the value back.

    Taken out of a class dict instead, it passes for its value: every protocol
    the value answers is answered from the value, and each use is noted as a
    read and as a bypass event. Identity tests and `type(x)` cannot be made
    faithful; a pair used that way would be left out by the build.
    """

    __slots__ = ("_br_pair", "_br_value")

    def __init__(self, pair: str, value) -> None:
        self._br_pair = pair
        self._br_value = value

    def __get__(self, obj, objtype=None):
        READS.add(self._br_pair)
        return self._br_value

    @property
    def __class__(self):
        _bypass(self, "__class__")
        return type(self._br_value)

    def __getattr__(self, name):
        _bypass(self, "getattr")
        return getattr(self._br_value, name)


def _delegate(name: str):
    def method(self, *args, **kwargs):
        _bypass(self, name)
        return getattr(self._br_value, name)(*args, **kwargs)

    method.__name__ = name
    return method


_PROTOCOLS = (
    "__iter__ __next__ __len__ __bool__ __contains__ __getitem__ __setitem__ __delitem__ __eq__ __ne__ "
    "__lt__ __le__ __gt__ __ge__ __hash__ __call__ __repr__ __str__ __format__ __or__ __ror__ __and__ "
    "__rand__ __xor__ __rxor__ __sub__ __rsub__ __add__ __radd__ __mul__ __rmul__ __int__ __index__ "
    "__float__ __bytes__ __reversed__ __fspath__ __sizeof__ __copy__ __deepcopy__ __reduce_ex__ "
    "__reduce__ __getstate__ __length_hint__ __ior__ __iand__ __iadd__ __missing__ __enter__ __exit__"
).split()
_PROXIES: dict = {}


def _proxy(value) -> type:
    """A stand-in class that has exactly the protocols the value's type has."""
    kind = type(value)
    proxy = _PROXIES.get(kind)
    if proxy is None:
        entries = {"__slots__": ()}
        for name in _PROTOCOLS:
            if hasattr(kind, name):
                entries[name] = None if getattr(kind, name) is None else _delegate(name)
        proxy = _PROXIES[kind] = type("_Logged", (_Logged,), entries)
    return proxy


def _resolve(module_name: str, qualname: str):
    """The class the module reaches by that name, the way state.Comparer._resolve walks it."""
    found = sys.modules.get(module_name)
    if found is None:
        return None
    for part in qualname.split("."):
        try:
            found = vars(found)[part]
        except (KeyError, TypeError):
            return None
    return found


def preload() -> None:
    """Import the whole package, in the order tools/trace/sitecustomize.py does."""
    base = os.path.join(ROOT, PACKAGE)
    for directory, subdirs, files in os.walk(base):
        subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py") and name != "__main__.py":
                parts = os.path.relpath(os.path.join(directory, name), ROOT)[:-3].split(os.sep)
                if parts[-1] == "__init__":
                    parts.pop()
                try:
                    importlib.import_module(".".join(parts))
                except BaseException:  # noqa: BLE001
                    pass


def package_classes() -> list:
    """Classes of the package: through module globals, nested class dicts and subclasses."""
    seen: dict = {}
    stack: list = []

    def consider(value) -> None:
        if isinstance(value, type) and str(getattr(value, "__module__", "")).startswith(PACKAGE):
            if id(value) not in seen:
                seen[id(value)] = value
                stack.append(value)

    for name, module in list(sys.modules.items()):
        if module is not None and (name == PACKAGE or name.startswith(PACKAGE + ".")):
            for value in list(vars(module).values()):
                consider(value)
    while stack:
        cls = stack.pop()
        for value in list(vars(cls).values()):
            consider(value)
        try:
            for sub in cls.__subclasses__():
                consider(sub)
        except TypeError:
            pass
    return list(seen.values())


def wrap_attributes() -> dict:
    """Install the descriptors. Returns {"pairs": [...], counts of what was left alone}."""
    from selector import state  # the one definition of what can be watched

    pairs: list = []
    left = {"classes": 0, "enum": 0, "unreachable": 0, "kept": 0, "unreachable_names": []}
    for cls in package_classes():
        if issubclass(cls, enum.Enum):
            left["enum"] += 1
            continue
        module_name, qualname = cls.__module__, cls.__qualname__
        if _resolve(module_name, qualname) is not cls:
            left["unreachable"] += 1
            left["unreachable_names"].append(f"{module_name}:{qualname}")
            continue
        left["classes"] += 1
        for name, value in list(vars(cls).items()):
            kind = type(value)
            if name in _NOISE or not state.watchable(cls, name, value) or any(hasattr(kind, hook) for hook in ("__set__", "__delete__")):
                left["kept"] += 1
                continue
            pair = f"{module_name}:{qualname}|{name}"
            try:
                setattr(cls, name, _proxy(value)(pair, value))
            except (AttributeError, TypeError):
                left["kept"] += 1
                continue
            pairs.append(pair)
    left["pairs"] = sorted(pairs)
    return left


# ---- the child ---------------------------------------------------------------


def run_in_child(write_fd: int, jobs: list) -> None:
    """Never returns. `jobs`: [(number, id)]; the last one is the test traced, the others run first."""
    try:
        os.setpgid(0, 0)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        from selector import runner

        READS.clear()
        verdicts = []
        for number, test_id in jobs:
            messages: list = []
            runner.run_tests([(number, test_id)], messages.append)
            finished = [line.split() for line in messages if line.startswith("R ")]
            verdicts.append(bool(finished) and all(line[2] == "0" for line in finished))
        payload = {
            "ok": verdicts[-1],
            "pairs": sorted(READS),
            "bypass": sorted(BYPASS),
            "cpu": round(sum(os.times()[:2]), 2),
        }
        data = json.dumps(payload, separators=(",", ":")).encode()
        view = memoryview(data)
        while view:
            view = view[os.write(write_fd, view):]
    except BaseException as error:  # noqa: BLE001 - the parent marks the test as not ok
        try:
            os.write(write_fd, json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"[:300]}).encode())
        except OSError:
            pass
    finally:
        os._exit(0)


def run_test(jobs: list, timeout: float) -> dict:
    read_fd, write_fd = os.pipe()
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        run_in_child(write_fd, jobs)
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
        os.killpg(pid, signal.SIGKILL)  # whatever the test left running
    except OSError:
        pass
    try:
        result = json.loads(b"".join(chunks))
    except ValueError:
        result = {"ok": False, "error": "timeout" if timed_out else f"died, status {status}"}
    result["wall"] = round(time.monotonic() - started, 2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--first", type=int, default=0)
    parser.add_argument("--last", type=int, default=1 << 30)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--no-wrap", action="store_true", help="baseline: same runs, nothing replaced")
    parser.add_argument("--only", default="", help="numbers of the only tests to run (a supplementary run, see build_areaders +DIR)")
    parser.add_argument("--warm", default="", help="numbers of tests to run first in the same process")
    arguments = parser.parse_args()
    arguments.out = os.path.abspath(arguments.out)
    if os.environ.get("PYTHONHASHSEED") != str(arguments.seed):
        os.environ["PYTHONHASHSEED"] = str(arguments.seed)
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path[:] = [ROOT, os.path.dirname(HERE)] + [entry for entry in sys.path if entry not in ("", HERE)]
    os.chdir(ROOT)
    from selector import runner

    runner._cap_duckdb_threads()
    ids = json.load(open(os.path.join(os.path.dirname(HERE), "maps", "tests.json")))
    preload()
    info = {"classes": 0, "pairs": []}
    if not arguments.no_wrap:
        info = wrap_attributes()
    os.makedirs(arguments.out, exist_ok=True)
    with open(os.path.join(arguments.out, "pairs.json"), "w") as handle:
        json.dump(info, handle, separators=(",", ":"))
    print({key: value for key, value in info.items() if key not in ("pairs", "unreachable_names")}, len(info["pairs"]), "pairs", flush=True)

    results_path = os.path.join(arguments.out, "results.jsonl")
    done = set()
    if os.path.exists(results_path):
        done = {json.loads(line)["n"] for line in open(results_path)}
    numbers = range(arguments.first, min(arguments.last, len(ids)))
    if arguments.only:
        numbers = [int(part) for part in arguments.only.split(",") if part]
    warm = [int(part) for part in arguments.warm.split(",") if part]
    with open(results_path, "a") as log:
        for number in numbers:
            if number in done:
                continue
            jobs = [(other, ids[other]) for other in warm] + [(number, ids[number])]
            result = run_test(jobs, arguments.timeout)
            result.update({"n": number, "id": ids[number], "warm": warm})
            log.write(json.dumps(result, separators=(",", ":")) + "\n")
            log.flush()
            if not result["ok"]:
                print("NOT OK", number, ids[number], result.get("error", ""), flush=True)


if __name__ == "__main__":
    main()
