"""Record what sqlglot's own executor returns for the TPC queries, and what code produces it.

    BR_TRACE_DIR=<dir> PYTHONPATH=/sqlglot:/tools/trace python build_executor_memo.py \
        /out/maps/execmemo.bin /out/maps/execphase.json

test_execute_tpch and test_execute_tpcds send every TPC query through
`sqlglot.executor.execute`: optimize, plan, run the plan. Two things are
recorded at the pinned commit:

  execmemo.bin    (names of the tables, digest of the optimized query's tree) -> the result table
  execphase.json  every line `execute` runs outside the optimizer: preparing
                  the tables it was given, planning and running the plan

The selector reuses a recorded result only for a patch that changes none of
the lines in execphase.json, and only for a query whose optimized form is
exactly the recorded one, run on tables that hold exactly the pinned data.
Then nothing that decides the result has changed.
"""
import json
import os
import pickle
import shutil
import sys
import zlib

sys.path.insert(0, "/repo")
from selector import runner  # noqa: E402

memo_path, phase_path = sys.argv[1], sys.argv[2]
attrs_path = sys.argv[3] if len(sys.argv) > 3 else None
trace_dir = os.environ.get("BR_TRACE_DIR")
record_dir = "/tmp/execmemo-records"
shutil.rmtree(record_dir, ignore_errors=True)
os.makedirs(record_dir)

os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up("/sqlglot", ["tests/test_executor.py"])
tracer = sys.modules.get("sitecustomize")
runner.MEMO_RECORD = record_dir
if trace_dir and hasattr(tracer, "_phase"):
    runner.MEMO_PHASE = tracer._phase
# The key of a result names the tables it was computed on: only tables holding
# exactly what these hold are recognised.
runner.build_executor_tables()
runner.install_executor_memo()

# Much of sqlglot's state is built on first use. Whatever is first used inside
# the phase is recorded as part of it, so the tests are run in every order a
# child can meet them in, each in a process of its own that has done nothing
# yet: both tests, either one alone, and the two the other way round.
names = list(runner.MEMO_TESTS)
for order in (names, names[::-1], names[:1], names[1:]):
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            ids = [f"tests/test_executor.py::TestExecutor::{name}" for name in order]
            messages = []
            runner.run_tests(list(enumerate(ids)), messages.append)
            failed = [message for message in messages if message.startswith("R") and message.endswith(" 1\n")]
            if failed:
                print(f"tests failed while recording: {failed}", flush=True)
                code = 1
            if hasattr(tracer, "_dump"):
                tracer._dump()
        except BaseException as error:  # noqa: BLE001
            print(f"recording raised {error!r}", flush=True)
            code = 1
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        raise SystemExit(f"recording {order} failed")

memo = {}
for name in os.listdir(record_dir):
    with open(os.path.join(record_dir, name), "rb") as handle:
        key, data = pickle.load(handle)
    known = memo.setdefault(key, data)
    if known != data:
        one, two = pickle.loads(known), pickle.loads(data)
        if one.columns != two.columns or one.rows != two.rows:
            raise SystemExit(f"two results for {key}")
with open(memo_path, "wb") as handle:
    handle.write(zlib.compress(pickle.dumps(memo, protocol=4), 9))
print(f"{len(memo)} results, {os.path.getsize(memo_path)} bytes")

if attrs_path:
    # Which class attributes does the phase read? Every plain data attribute of
    # every class of sqlglot gets a descriptor that notes a read (through the
    # class, a subclass or an instance) while the phase is on. The whole
    # package is imported first, so that classes of modules imported on demand
    # are watched too, and the tests run in the same orders as above, each in a
    # fresh process: what is built on first use is then read inside the phase
    # by whichever test comes first.
    from selector import state  # noqa: E402

    read_dir = "/tmp/execattrs-records"
    shutil.rmtree(read_dir, ignore_errors=True)
    os.makedirs(os.path.join(read_dir, "memo"))
    watcher = os.fork()
    if watcher == 0:
        code = 0
        try:
            import importlib

            for name in state.package_modules("/sqlglot"):
                try:
                    importlib.import_module(name)
                except BaseException:  # noqa: BLE001
                    pass
            reads: set = set()
            recording = [False]

            class Watch:
                __slots__ = ("value", "key")

                def __init__(self, value, key):
                    self.value = value
                    self.key = key

                def __get__(self, instance, owner=None):
                    if recording[0]:
                        reads.add(self.key)
                    return self.value

            watched = {}
            for module in list(sys.modules.values()):
                module_name = getattr(module, "__name__", "")
                if not (module_name == "sqlglot" or module_name.startswith("sqlglot.")):
                    continue
                pending = [v for v in vars(module).values() if isinstance(v, type) and v.__module__ == module_name]
                while pending:
                    cls = pending.pop()
                    for attr, value in list(vars(cls).items()):
                        if isinstance(value, type):
                            if value.__module__ == module_name and value.__qualname__.startswith(cls.__qualname__ + "."):
                                pending.append(value)
                            continue
                        key = f"{module_name}:{cls.__qualname__}|{attr}"
                        if key in watched or not state.watchable(cls, attr, value):
                            continue
                        try:
                            type.__setattr__(cls, attr, Watch(value, key))
                        except (AttributeError, TypeError):
                            reads.add(key)  # cannot be watched: counts as read
                            continue
                        watched[key] = (cls, attr)
            runner.MEMO_RECORD = os.path.join(read_dir, "memo")
            runner.MEMO_PHASE = lambda kind: recording.__setitem__(0, kind == "X")
            for number, order in enumerate((names, names[::-1], names[:1], names[1:])):
                pid = os.fork()
                if pid == 0:
                    inner = 0
                    try:
                        ids = [f"tests/test_executor.py::TestExecutor::{name}" for name in order]
                        messages = []
                        runner.run_tests(list(enumerate(ids)), messages.append)
                        failed = [m for m in messages if m.startswith("C") or (m.startswith("R") and m.endswith(" 1\n"))]
                        if failed:
                            print(f"tests failed while watching attributes: {failed}", flush=True)
                            inner = 1
                        # An attribute that was assigned anew is no longer watched: count it as read.
                        gone = [key for key, (cls, attr) in watched.items() if type(vars(cls).get(attr)) is not Watch]
                        with open(os.path.join(read_dir, f"reads-{number}.json"), "w") as handle:
                            json.dump(sorted(reads | set(gone)), handle)
                    except BaseException as error:  # noqa: BLE001
                        print(f"watching raised {error!r}", flush=True)
                        inner = 1
                    os._exit(inner)
                _, status = os.waitpid(pid, 0)
                if status != 0:
                    code = 1
            with open(os.path.join(read_dir, "watched.json"), "w") as handle:
                json.dump(len(watched), handle)
        except BaseException as error:  # noqa: BLE001
            print(f"watching raised {error!r}", flush=True)
            code = 1
        os._exit(code)
    _, status = os.waitpid(watcher, 0)
    if status != 0:
        raise SystemExit("recording the attributes the phase reads failed")
    read = set()
    for name in os.listdir(read_dir):
        if name.startswith("reads-"):
            read.update(json.load(open(os.path.join(read_dir, name))))
    json.dump({"read": sorted(read)}, open(attrs_path, "w"), separators=(",", ":"))
    print(f"{json.load(open(os.path.join(read_dir, 'watched.json')))} class attributes watched, {len(read)} read in the phase")

if trace_dir:
    if hasattr(tracer, "_dump"):
        tracer._dump()
    units = {}
    for name in os.listdir(trace_dir):
        with open(os.path.join(trace_dir, name), encoding="utf-8") as handle:
            for row in handle:
                if row.startswith("X "):
                    _, path, first, line = row.rstrip("\n").rsplit(" ", 3)
                    units.setdefault(path[len("/sqlglot/"):], set()).add((int(first), int(line)))
    json.dump({path: sorted(map(list, rows)) for path, rows in sorted(units.items())},
              open(phase_path, "w"), separators=(",", ":"))
    print({path: len(rows) for path, rows in sorted(units.items())})
