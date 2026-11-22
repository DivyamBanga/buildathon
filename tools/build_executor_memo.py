"""Record what sqlglot's own executor returns for the TPC queries, and what produces it.

    BR_TRACE_DIR=<dir> PYTHONPATH=/sqlglot:/tools/trace python build_executor_memo.py \
        /out/maps/execmemo.bin /out/maps/execphase.json /out/maps/execattrs.json /out/maps/execqueries.bin

test_execute_tpch and test_execute_tpcds send every TPC query through
`sqlglot.executor.execute`: optimize, plan, run the plan. Recorded at the
pinned commit:

  execmemo.bin     (names of the tables, digest of the optimized query's tree) -> the result table
  execphase.json   every line `execute` runs outside the optimizer: preparing
                   the tables it was given, planning and running the plan
  execattrs.json   every plain data attribute of a sqlglot class that those lines read
  execqueries.bin  the same two, query by query: memo key -> (which of the
                   lines, which of the attributes) as bit masks over the two
                   lists, the digests of the slots that query reads (type
                   annotation, comments, meta; from tools/trace_slots.py,
                   whose slots.json is the optional fifth argument), and for
                   an attribute that holds a table (a dict) and is only ever
                   looked up by key, the names of those keys (tools/recdict.py)
  execfront.json   (optional sixth argument) {"lines": ...}: every line that runs
                   between the start of the call `optimize(...)` inside `execute`
                   and the start of the planner (the window F), in the shape of
                   execphase.json
  execfrontq.bin   (optional seventh argument) the key BEFORE the optimizer,
                   query by query: runner.front_key of the call -> (the digest
                   of the optimized tree, i.e. the last part of the memo key;
                   which of the lines of execfront.json that query runs, as a
                   bit mask over them in sorted order)

A query's lines are those of its own fresh-fork run together with those it runs
inside each whole-test order (a query that follows others can take other paths:
a table already filled, a value already cached). The tracer's file of a process
says which rows belong to which call through marker rows written here: `B` and `E`
around every call of `execute` that the test makes, `K <memo key>` when its key is known.

The selector reuses a recorded result only for a query whose optimized form is
exactly the recorded one, run on tables that hold exactly the pinned data, and
only when the patch changes none of the lines that query runs in that phase
and nothing those lines read. Then nothing that decides the result has changed.

Much of sqlglot's state is built on first use, and whatever is first used
inside the phase belongs to it. So every recording is made in a process that
has done nothing yet: the two tests in every order a child can meet them in
(both, reversed, each alone), and every query on its own.
"""
import ast
import json
import marshal
import os
import pickle
import shutil
import sys
import zlib

sys.path.insert(0, "/repo")
from selector import runner, state  # noqa: E402

memo_path, phase_path = sys.argv[1], sys.argv[2]
attrs_path = sys.argv[3] if len(sys.argv) > 3 else None
queries_path = sys.argv[4] if len(sys.argv) > 4 else None
slots_path = sys.argv[5] if len(sys.argv) > 5 else None
front_path = sys.argv[6] if len(sys.argv) > 6 else None
frontq_path = sys.argv[7] if len(sys.argv) > 7 else None
trace_dir = os.environ.get("BR_TRACE_DIR")
work_dir = "/tmp/execmemo-work"
shutil.rmtree(work_dir, ignore_errors=True)
record_dir = os.path.join(work_dir, "results")
os.makedirs(record_dir)

os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up("/sqlglot", ["tests/test_executor.py"])
tracer = sys.modules.get("sitecustomize")
runner.MEMO_RECORD = record_dir


def mark(text) -> None:
    """A marker row in the line tracer's file of this process, after what it has written so far."""
    directory = getattr(tracer, "_DIR", None)
    if directory:
        with open(os.path.join(directory, f"{os.getpid()}.txt"), "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


if trace_dir and hasattr(tracer, "_phase"):
    runner.MEMO_PHASE = tracer._phase

# The test calls `execute` by the name its module imported: every call is put between two marker rows.
_test_module = sys.modules["tests.test_executor"]
_test_execute = _test_module.execute


def _marked_execute(*args, **kwargs):
    mark("B")
    try:
        return _test_execute(*args, **kwargs)
    finally:
        mark("E")


_test_module.execute = _marked_execute
# The key of a result names the tables it was computed on: only tables holding
# exactly what these hold are recognised.
runner.build_executor_tables()
runner.install_executor_memo()

NAMES = list(runner.MEMO_TESTS)
# The executor's function table, as one more "attribute" every query reads.
ENV_PAIR = "sqlglot.executor.env:|ENV"
ORDERS = (NAMES, NAMES[::-1], NAMES[:1], NAMES[1:])
FIXTURES = {"tpch": "optimizer/tpc-h/tpc-h.sql", "tpcds": "optimizer/tpc-ds/tpc-ds.sql"}
helpers = sys.modules["tests.helpers"]
CASES = [
    (scope, name, index)
    for name, scope in runner.MEMO_TESTS.items()
    for index in range(len(list(helpers.load_sql_fixture_pairs(FIXTURES[scope]))))
]


def in_fork(work) -> None:
    """Run work() in a forked process; stop everything if it fails there."""
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            work()
        except BaseException as error:  # noqa: BLE001
            print(f"recording raised {error!r}", flush=True)
            code = 1
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        raise SystemExit("a recording failed")


def run(order, case=None) -> list:
    """Run the named tests here (with one fixture case only, if given); the memo keys of the queries executed."""
    keys = []
    real_key = runner.memo_key

    def noting(*args, **kwargs):
        key = real_key(*args, **kwargs)
        keys.append(key)
        if key is not None:
            mark(f"K {key}")
        return key

    runner.memo_key = noting
    if case is not None:
        scope, _name, index = case
        runner.CASE_FILTER[("pairs", FIXTURES[scope])] = frozenset([index])
    ids = [f"tests/test_executor.py::TestExecutor::{name}" for name in order]
    messages = []
    runner.run_tests(list(enumerate(ids)), messages.append)
    failed = [m for m in messages if m.startswith("C") or (m.startswith("R") and m.endswith(" 1\n"))]
    if failed:
        raise RuntimeError(f"tests failed while recording: {failed}")
    if None in keys:
        raise RuntimeError("a query ran on tables that are not the pinned ones")
    return keys


# ---- results, and the lines the phase runs ---------------------------------

def record_lines(order, case, out) -> None:
    keys = run(order, case)
    if hasattr(tracer, "_dump"):
        tracer._dump()
    with open(out, "w") as handle:
        json.dump({"pid": os.getpid(), "keys": keys}, handle)


for number, order in enumerate(ORDERS):
    in_fork(lambda: record_lines(order, None, os.path.join(work_dir, f"lines-order-{number}.json")))
if queries_path:
    # One query at a time; the class setup takes its tables ready-made.
    runner._use_executor_tables()
    for number, case in enumerate(CASES):
        in_fork(lambda: record_lines([case[1]], case, os.path.join(work_dir, f"lines-case-{number}.json")))

memo = {}
front_of = {}  # the key before the optimizer -> the memo key of the tree optimize made of it
slots_of = {}  # memo key -> the digests of what its tree holds in each slot of runner.SLOTS
for name in os.listdir(record_dir):
    with open(os.path.join(record_dir, name), "rb") as handle:
        key, slots, data, *more = pickle.load(handle)
    if more and more[0] is not None:
        # runner.front_key of the call of optimize that made this tree
        if front_of.setdefault(more[0], key) != key:
            raise SystemExit(f"one key before the optimizer, two optimized trees: {more[0]}")
    if slots_of.setdefault(key, slots) != slots:
        raise SystemExit(f"two trees with other annotations, comments or meta for {key}")
    known = memo.setdefault(key, data)
    if known != data:
        one, two = pickle.loads(known), pickle.loads(data)
        if one.columns != two.columns or one.rows != two.rows:
            raise SystemExit(f"two results for {key}")
with open(memo_path, "wb") as handle:
    handle.write(zlib.compress(pickle.dumps(memo, protocol=4), 9))
print(f"{len(memo)} results, {os.path.getsize(memo_path)} bytes")

rows_of_pid = {}
if trace_dir:
    if hasattr(tracer, "_dump"):
        tracer._dump()
    front_rows_of_pid = {}
    calls_of_pid = {}  # pid -> [(memo key or None, X rows, F rows)] for every call of execute, in order
    for name in os.listdir(trace_dir):
        pid = name.split(".")[0]
        found = rows_of_pid.setdefault(pid, set())
        front_found = front_rows_of_pid.setdefault(pid, set())
        calls = calls_of_pid.setdefault(pid, [])
        call = None  # [key, X rows, F rows] of the call of execute that is under way
        with open(os.path.join(trace_dir, name), encoding="utf-8") as handle:
            for row in handle:
                letter = row[:2]
                if row.rstrip() == "B":
                    call = [None, set(), set()]
                elif row.rstrip() == "E":
                    if call is not None:
                        calls.append(tuple(call))
                    call = None
                elif letter == "K ":
                    if call is not None:
                        call[0] = row[2:].rstrip("\n")
                elif letter in ("X ", "F "):
                    parts = row.rstrip("\n").rsplit(" ", 3)
                    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit() or not parts[1].startswith("/sqlglot/"):
                        continue
                    entry = (parts[1][len("/sqlglot/"):], int(parts[2]), int(parts[3]))
                    (found if letter == "X " else front_found).add(entry)
                    if call is not None:
                        call[1 if letter == "X " else 2].add(entry)
        if call is not None:
            calls.append(tuple(call))
    every = sorted(set().union(*rows_of_pid.values())) if rows_of_pid else []
    units = {}
    for path, first, line in every:
        units.setdefault(path, []).append([first, line])
    json.dump(units, open(phase_path, "w"), separators=(",", ":"))
    print({path: len(rows) for path, rows in units.items()})
    row_index = {row: index for index, row in enumerate(every)}

    # ---- what a query runs when it is not the first thing its process does --------
    case_pids = set()
    order_pids = set()
    for name in os.listdir(work_dir):
        if name.startswith("lines-case-"):
            case_pids.add(str(json.load(open(os.path.join(work_dir, name)))["pid"]))
        elif name.startswith("lines-order-"):
            order_pids.add(str(json.load(open(os.path.join(work_dir, name)))["pid"]))
    cold_rows, warm_rows = {}, {}  # memo key -> (X rows, F rows), from its own run and from the whole-test orders
    for pid, calls in calls_of_pid.items():
        target = cold_rows if pid in case_pids else warm_rows if pid in order_pids else None
        if target is None:
            continue
        for key, x_rows, o_rows in calls:
            if key is None:
                continue
            both = target.setdefault(key, (set(), set()))
            both[0].update(x_rows)
            both[1].update(o_rows)
    extra_x = {key: rows[0] - cold_rows.get(key, (set(), set()))[0] for key, rows in warm_rows.items()}
    extra_o = {key: rows[1] - cold_rows.get(key, (set(), set()))[1] for key, rows in warm_rows.items()}
    print(f"rows a query runs only inside a whole-test order: phase X {sum(1 for rows in extra_x.values() if rows)} queries, "
          f"{len(set().union(*extra_x.values()) if extra_x else ())} rows; window F {sum(1 for rows in extra_o.values() if rows)} queries, "
          f"{len(set().union(*extra_o.values()) if extra_o else ())} rows")
    for key, rows in sorted(extra_x.items(), key=lambda item: -len(item[1]))[:3]:
        if rows:
            print(f"   X only when not first, {key[-12:]}: {sorted(rows)[:6]}")
    WARM_X = extra_x

    # ---- the key before the optimizer, query by query ------------------------
    if front_path and frontq_path and queries_path:
        every_front = sorted(set().union(*front_rows_of_pid.values())) if front_rows_of_pid else []
        front_index = {row: index for index, row in enumerate(every_front)}
        front_units = {}
        for path, first, line in every_front:
            front_units.setdefault(path, []).append([first, line])
        unplaced = set(every_front) - set().union(*(rows[1] for rows in cold_rows.values()), *(rows[1] for rows in warm_rows.values()))
        if unplaced:
            # Rows of the window that no call owns: they count for every query.
            print(f"{len(unplaced)} rows of the window before the optimizer belong to no call: every query gets them")

        def front_mask(rows) -> bytes:
            value = 0
            for row in rows:
                value |= 1 << front_index[row]
            return value.to_bytes((value.bit_length() + 7) // 8 or 1, "little")

        fronts = {}
        for name, key in sorted(front_of.items()):
            if key not in cold_rows:
                raise SystemExit(f"no run of its own for the query behind {name}")
            rows = cold_rows[key][1] | warm_rows.get(key, (set(), set()))[1] | unplaced
            fronts[name] = (key.rsplit(":", 1)[1], front_mask(rows))
        json.dump({"lines": front_units}, open(front_path, "w"), separators=(",", ":"))
        with open(frontq_path, "wb") as handle:
            handle.write(zlib.compress(marshal.dumps(fronts), 9))
        sizes = sorted(int.from_bytes(mask, "little").bit_count() for _digest, mask in fronts.values()) or [0]
        print(f"before the optimizer: {len(fronts)} keys for {len(memo)} results, {sizes[0]} to {sizes[-1]} of {len(every_front)} lines each, "
              f"{ {path: len(rows) for path, rows in front_units.items()} }, {os.path.getsize(frontq_path)} bytes")

# ---- the class attributes the phase reads ----------------------------------

read_dir = os.path.join(work_dir, "reads")
os.makedirs(os.path.join(read_dir, "results"))


def watch_and_record() -> None:
    """Every plain data attribute of every class of sqlglot gets a descriptor
    that notes a read (through the class, a subclass or an instance) while the
    phase is on. The whole package is imported first, so that the classes of
    modules imported on demand are watched too."""
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
    runner.MEMO_RECORD = os.path.join(read_dir, "results")
    import recdict

    # The executor's function table (ENV in sqlglot/executor/env.py) is copied
    # into every namespace that generated code is evaluated in, so every query
    # "reads" it. What a query uses of it are the entries its code names: the
    # names of every piece of code it evaluates (all of them: a name the table
    # does not hold falls through to the builtins, and a new entry of that
    # name would be found instead), and the key of the one lookup by a
    # computed name. That is a reading of the source, checked below (ENV_USES).
    import types

    import sqlglot.executor.context as context_module
    import sqlglot.executor.python as python_module

    env_names: set = set()
    seen_code: set = set()

    def code_names(code) -> set:
        if isinstance(code, str):
            code = compile(code, "<eval>", "eval")
        found = set(code.co_names)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                found |= code_names(const)
        return found

    real_eval = context_module.Context.eval

    def noting_eval(self, code):
        if recording[0] and code not in seen_code:
            seen_code.add(code)
            env_names.update(code_names(code))
        return real_eval(self, code)

    context_module.Context.eval = noting_eval
    real_comparison = python_module.PythonExecutor._subquery_comparison

    def noting_comparison(self, value, plan_name, scope, op, quantifier, *args):
        if recording[0]:
            env_names.add(op)
        return real_comparison(self, value, plan_name, scope, op, quantifier, *args)

    python_module.PythonExecutor._subquery_comparison = noting_comparison

    descriptors = {key: vars(cls)[attr] for key, (cls, attr) in watched.items()}
    del value, pending  # nothing here may still hold a table (see recdict.wrap)
    key_pairs: list = []
    lines_phase = getattr(tracer, "_phase", None)
    noted = {"keys": set(), "whole": set()}

    def phase(kind) -> None:
        on = kind == "X"
        if on != recording[0]:
            if on:
                recdict.open_window()
            else:
                keys, whole = recdict.close_window()
                noted["keys"] |= keys
                noted["whole"] |= whole
        recording[0] = on
        if lines_phase is not None:
            lines_phase(kind)

    runner.MEMO_PHASE = phase

    def record_reads(order, case, out) -> None:
        keys = run(order, case)
        if hasattr(tracer, "_dump"):
            tracer._dump()
        # An attribute that was assigned anew is no longer watched: count it as read.
        gone = [key for key, (cls, attr) in watched.items() if type(vars(cls).get(attr)) is not Watch]
        # A table that was only ever asked for single keys: which keys. A table
        # that was walked, measured, copied or compared, or fetched and never
        # looked into (passed on, compared by identity), counts as read whole.
        by_table: dict = {}
        for table_id, key in noted["keys"]:
            by_table.setdefault(table_id, []).append(key)
        keyed = {}
        for pair in key_pairs:
            table = descriptors[pair].value
            if pair not in reads or id(table) in noted["whole"] or id(table) not in by_table:
                continue
            names: set = set()
            for key in by_table[id(table)]:
                # A lookup that found an entry is named by the key the table holds.
                found = state.key_names(recdict.stored(table, key))
                if found is None:
                    names = None
                    break
                names |= found
            if names is not None:
                keyed[pair] = sorted(names)
        with open(out, "w") as handle:
            json.dump(
                {"pid": os.getpid(), "keys": keys, "reads": sorted(reads | set(gone)), "keyed": keyed, "env": sorted(env_names)},
                handle,
            )

    if queries_path and hasattr(tracer, "_DIR"):
        # Once with the tables as they are: the lines each query runs here, to be
        # compared with the lines it runs when recording stand-ins are in place.
        tracer._DIR = os.path.join(read_dir, "rows-plain")
        os.makedirs(tracer._DIR)
        for number, case in enumerate(CASES):
            in_fork(lambda: record_reads([case[1]], case, os.path.join(read_dir, f"plain-case-{number}.json")))
        tracer._DIR = os.path.join(read_dir, "rows-keyed")
        os.makedirs(tracer._DIR)
        found_pairs, left_whole = recdict.wrap(descriptors, slot="value")
        key_pairs.extend(found_pairs)
        with open(os.path.join(read_dir, "tables.json"), "w") as handle:
            json.dump({"keyed": found_pairs, "whole": left_whole}, handle)
    for number, order in enumerate(ORDERS):
        in_fork(lambda: record_reads(order, None, os.path.join(read_dir, f"reads-order-{number}.json")))
    if queries_path:
        # The class setup already takes its tables ready-made (set up above, before this process was forked).
        for number, case in enumerate(CASES):
            in_fork(lambda: record_reads([case[1]], case, os.path.join(read_dir, f"reads-case-{number}.json")))
    with open(os.path.join(read_dir, "watched.json"), "w") as handle:
        json.dump(len(watched), handle)


if attrs_path:
    in_fork(watch_and_record)
    recorded = {
        name: json.load(open(os.path.join(read_dir, name)))
        for name in os.listdir(read_dir)
        if name.startswith("reads-")
    }
    read = sorted(set().union(*(entry["reads"] for entry in recorded.values())) | {ENV_PAIR})
    # Every line of the package that mentions the function table or a copy of it:
    # the selector uses the per-query entries only while this is the list it reviewed.
    env_uses = []
    for name in state.package_modules("/sqlglot"):
        source_path = "/sqlglot/" + name.replace(".", "/") + ".py"
        if not os.path.exists(source_path):
            source_path = "/sqlglot/" + name.replace(".", "/") + "/__init__.py"
        try:
            module_tree = ast.parse(open(source_path, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(module_tree):
            if (isinstance(node, ast.Name) and node.id == "ENV") or (isinstance(node, ast.Attribute) and node.attr == "env"):
                env_uses.append([source_path[len("/sqlglot/"):], node.lineno])
            elif isinstance(node, ast.alias) and node.name == "ENV":
                env_uses.append([source_path[len("/sqlglot/"):], node.lineno])
    env_uses = sorted(map(list, {tuple(use) for use in env_uses}))
    json.dump({"read": read, "env_uses": env_uses}, open(attrs_path, "w"), separators=(",", ":"))
    print(f"lines that mention the executor's function table: {env_uses}")
    print(f"{json.load(open(os.path.join(read_dir, 'watched.json')))} class attributes watched, {len(read)} read in the phase")
    read_index = {key: index for index, key in enumerate(read)}

# ---- query by query --------------------------------------------------------

if queries_path and trace_dir and attrs_path:
    def mask(indexes) -> bytes:
        value = 0
        for index in indexes:
            value |= 1 << index
        return value.to_bytes((value.bit_length() + 7) // 8 or 1, "little")

    def phase_rows(directory, pid) -> set:
        """The phase lines one process ran, from the line tracer's file for it."""
        found = set()
        try:
            with open(os.path.join(read_dir, directory, f"{pid}.txt"), encoding="utf-8") as handle:
                for row in handle:
                    if row.startswith("X "):
                        found.add(row)
        except OSError:
            return {"no trace"}
        return found

    lines_of, reads_of, keyed_of = {}, {}, {}
    diverged = 0
    for number, case in enumerate(CASES):
        entry = json.load(open(os.path.join(work_dir, f"lines-case-{number}.json")))
        watched_entry = recorded[f"reads-case-{number}.json"]
        if entry["keys"] != watched_entry["keys"] or len(entry["keys"]) > 1:
            raise SystemExit(f"case {case}: keys {entry['keys']} and {watched_entry['keys']}")
        keyed = watched_entry.get("keyed", {})
        plain_file = os.path.join(read_dir, f"plain-case-{number}.json")
        if entry["keys"]:
            # The stand-ins must not have changed what the query runs: the same
            # lines with and without them, or every table counts as read whole.
            plain_entry = json.load(open(plain_file)) if os.path.exists(plain_file) else None
            same = (
                plain_entry is not None
                and plain_entry["keys"] == watched_entry["keys"]
                and plain_entry["reads"] == watched_entry["reads"]
                and plain_entry.get("env") == watched_entry.get("env")
                and phase_rows("rows-plain", plain_entry["pid"]) == phase_rows("rows-keyed", watched_entry["pid"])
            )
            if not same:
                diverged += 1
                keyed = {}
            elif "env" in watched_entry:
                # The entries of the function table the query's code names (see watch_and_record).
                names = set()
                for name in watched_entry["env"]:
                    names |= state.key_names(name)
                keyed = dict(keyed)
                keyed[ENV_PAIR] = sorted(names)
        for key in entry["keys"]:
            lines_of.setdefault(key, set()).update(rows_of_pid.get(str(entry["pid"]), ()))
            reads_of.setdefault(key, set()).update(watched_entry["reads"])
            reads_of[key].add(ENV_PAIR)
            if key in keyed_of:
                # The same query twice: a table is keyed only if it was both times.
                keyed_of[key] = {pair: sorted(set(names) | set(keyed[pair])) for pair, names in keyed_of[key].items() if pair in keyed}
            else:
                keyed_of[key] = dict(keyed)
    if set(lines_of) != set(memo):
        raise SystemExit(f"queries recorded one by one: {len(lines_of)}, results: {len(memo)}")
    # Does planning and executing a query ask a node for its type? Only then do
    # the annotations of its tree matter to the result. A node is asked through
    # Expr.is_type or the `type` property; any other executed line that reads
    # the `_type` slot (copying it along, in __deepcopy__, does not count)
    # makes every query count as asking.
    asking = set()
    stray = []
    for path in units:
        tree = ast.parse(open("/sqlglot/" + path, encoding="utf-8").read())
        functions = [
            (min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno, node.name)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        executed = {line for _first, line in units[path]}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "_type" and isinstance(node.ctx, ast.Load) and node.lineno in executed:
                holders = [(last - first, name) for first, last, name in functions if first <= node.lineno <= last]
                name = min(holders)[1] if holders else "<module>"
                if not (path == "sqlglot/expressions/core.py" and name in ("is_type", "type", "__deepcopy__")):
                    stray.append((path, node.lineno, name))
        if path == "sqlglot/expressions/core.py":
            for first, last, name in functions:
                if name in ("is_type", "type"):
                    asking.update(row for row in every if row[0] == path and first <= row[2] <= last)
    if stray:
        print(f"the type slot is read outside is_type and the type property: {stray[:5]}; every query counts as asking")
    # Which slots does planning and executing a query read on the nodes of its
    # tree (or on copies of them)? tools/trace_slots.py answers that with a
    # descriptor in each slot's place, query by query. A query it has no
    # answer for reads them all. For the annotation the line rule above
    # counts as well.
    traced = json.load(open(slots_path)) if slots_path else {}
    queries = {}
    for key in sorted(memo):
        found = traced.get(key)
        wanted = []
        for slot, digest in zip(runner.SLOTS, slots_of[key]):
            reads_it = found is None or bool(found.get(slot))
            if slot == "_type":
                reads_it = reads_it or bool(stray) or bool(lines_of[key] & asking)
            wanted.append(digest if reads_it else "")
        queries[key] = (
            mask(row_index[row] for row in lines_of[key]),
            mask(read_index[name] for name in reads_of[key]),
            tuple(wanted),
            {read_index[pair]: names for pair, names in sorted(keyed_of[key].items())},
        )
    for position, slot in enumerate(runner.SLOTS):
        print(f"{sum(1 for entry in queries.values() if entry[2][position])} of {len(queries)} queries read {slot}")
    tables = json.load(open(os.path.join(read_dir, "tables.json"))) if os.path.exists(os.path.join(read_dir, "tables.json")) else {}
    read_tables = sorted(pair for pair in tables.get("keyed", ()) if pair in read_index)
    by_key_only = sorted({pair for found in keyed_of.values() for pair in found})
    print(f"tables among the attributes read: {len(read_tables)} with a stand-in, "
          f"{sorted(pair for pair in tables.get('whole', {}) if pair in read_index)} left alone; "
          f"looked up by key only in some query: {by_key_only}; {diverged} queries ran other lines with stand-ins")
    if traced and set(traced) != set(memo):
        print(f"slot trace and record disagree on the queries: {len(set(traced) ^ set(memo))} keys in one only")
    with open(queries_path, "wb") as handle:
        handle.write(zlib.compress(marshal.dumps(queries), 9))
    sizes = sorted(len(rows) for rows in lines_of.values())
    print(f"{len(queries)} queries one by one: {sizes[0]} to {sizes[-1]} of {len(row_index)} lines each, "
          f"{min(len(v) for v in reads_of.values())} to {max(len(v) for v in reads_of.values())} of {len(read_index)} attributes, "
          f"{os.path.getsize(queries_path)} bytes")
