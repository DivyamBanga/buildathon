"""Checks of the key taken before the optimizer and of the way a recorded result is found by it (run in the lab image).

    PYTHONPATH=/sqlglot:/repo python /repo/tools/test_front.py

Three TPC-DS queries are executed once for real with recording on; their
records then stand for maps/execmemo.bin, execqueries.bin and execfrontq.bin,
and every way the front key may and may not be used is walked through,
counting how often the real optimizer and the real planner are called.
"""
import os
import pickle
import sys
import tempfile

sys.path.insert(0, "/repo")
os.environ["SKIP_INTEGRATION"] = "0"
from selector import runner  # noqa: E402

runner.warm_up("/sqlglot", ["tests/test_executor.py"])
import sqlglot.executor as executor  # noqa: E402
from sqlglot import find_tables, parse_one  # noqa: E402
from sqlglot.schema import ensure_schema  # noqa: E402

helpers = sys.modules["tests.helpers"]
checks = 0


def check(condition, what):
    global checks
    checks += 1
    if not condition:
        raise SystemExit(f"FAILED: {what}")


calls = {"optimize": 0, "plan": 0}
plain_optimize = executor.optimize
plain_plan = executor.Plan


def counting_optimize(*args, **kwargs):
    calls["optimize"] += 1
    return plain_optimize(*args, **kwargs)


def counting_plan(expression):
    calls["plan"] += 1
    return plain_plan(expression)


executor.optimize = counting_optimize
executor.Plan = counting_plan
runner.build_executor_tables()
runner.install_executor_memo()
TABLES = runner.EXECUTOR_TABLES[1]
SCHEMA = helpers.TPCDS_SCHEMA
ARGS = {"leave_tables_isolated": True, "dialect": None}
pairs = [sql for meta, sql, _ in helpers.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql") if meta.get("execute")]


def tree_of(sql):
    # As the test hands it over: parsed on its side, pickled on the way to the pool.
    return pickle.loads(pickle.dumps(parse_one(sql), pickle.HIGHEST_PROTOCOL))


def run(sql, schema=SCHEMA, tables=None, change=None):
    """What one `execute` call does to the two counters, and its result."""
    tree = tree_of(sql)
    if change is not None:
        change(tree)
    if tables is None:
        tables = {table.name: TABLES[table.name] for table in find_tables(tree)}
    before = dict(calls)
    result = executor.execute(tree, schema=schema, tables=tables)
    return calls["optimize"] - before["optimize"], calls["plan"] - before["plan"], result


def same(one, two) -> bool:
    return tuple(one.columns) == tuple(two.columns) and list(one.rows) == list(two.rows)


# ---- the key itself ---------------------------------------------------------
schema_object = ensure_schema(SCHEMA)
first = pairs[0]
key = runner.front_key("tpcds", (tree_of(first), schema_object), ARGS)
check(type(key) is str and key.startswith("tpcds:"), "a tree and a schema have a key")
check(key == runner.front_key("tpcds", (tree_of(first), ensure_schema(SCHEMA)), ARGS), "parsed again, with a schema built again: the same key")
check(key != runner.front_key("tpch", (tree_of(first), schema_object), ARGS), "the scope is part of the key")
check(key != runner.front_key("tpcds", (tree_of(pairs[1]), schema_object), ARGS), "another query has another key")
check(runner.front_key("tpcds", (first, schema_object), ARGS) is None, "a query given as text has no key")
check(runner.front_key("tpcds", (tree_of(first), schema_object), {"leave_tables_isolated": True}) is None, "a call without the dialect argument has none")
check(runner.front_key("tpcds", (tree_of(first), schema_object), dict(ARGS, dialect="duckdb")) is None, "nor one with a dialect")
check(runner.front_key("tpcds", (tree_of(first), schema_object), dict(ARGS, leave_tables_isolated=False)) is None, "nor one that does not isolate tables")
check(runner.front_key("tpcds", (tree_of(first), schema_object), dict(ARGS, validate_qualify_columns=False)) is None, "nor one with a third setting")
check(runner.front_key("tpcds", (tree_of(first), schema_object, None), ARGS) is None, "nor one with a third positional argument")
check(runner.front_key("tpcds", (tree_of(first), SCHEMA), ARGS) is None, "a schema that is a plain dict has none")
inner = tree_of(first).find_all(type(tree_of(first).expressions[0]))
check(runner.front_key("tpcds", (next(inner), schema_object), ARGS) is None, "a node inside a tree has none")
commented = tree_of(first)
commented.expressions[0].comments = ["a note"]
check(runner.front_key("tpcds", (commented, schema_object), ARGS) not in (None, key), "a comment is part of the key")
placed = tree_of(first)
placed.expressions[0].meta["line"] = 99
check(runner.front_key("tpcds", (placed, schema_object), ARGS) not in (None, key), "so is a position")
stale = tree_of(first)
stale._hash = hash(stale) + 1
check(runner.front_key("tpcds", (stale, schema_object), ARGS) is None, "a tree with a wrong cached hash has no key")
hashed = tree_of(first)
hash(hashed)
check(runner.front_key("tpcds", (hashed, schema_object), ARGS) == key, "one with true cached hashes has the key of the tree")
table_name = next(iter(SCHEMA))
reordered = dict(SCHEMA)
reordered[table_name] = dict(reversed(list(SCHEMA[table_name].items())))
check(runner.front_key("tpcds", (tree_of(first), ensure_schema(reordered)), ARGS) not in (None, key), "the order of a table's columns is part of the key")
retyped = dict(SCHEMA)
column = next(iter(SCHEMA[table_name]))
retyped[table_name] = dict(SCHEMA[table_name], **{column: "date" if SCHEMA[table_name][column] != "date" else "int"})
check(runner.front_key("tpcds", (tree_of(first), ensure_schema(retyped)), ARGS) not in (None, key), "so is a column's type")
fewer = {name: columns for name, columns in SCHEMA.items() if name != table_name}
check(runner.front_key("tpcds", (tree_of(first), ensure_schema(fewer)), ARGS) not in (None, key), "and which tables there are")
check(runner._Known("k", None, (), {}).sql(pretty=True) == "", "the stand-in answers the debug line with an empty text")

# ---- recording: three queries, executed for real ------------------------------
record_dir = tempfile.mkdtemp()
events: list = []
runner.MEMO_RECORD = record_dir
runner.MEMO_PHASE = events.append
runner.MEMO_SCOPE = "tpcds"
chosen = pairs[:3]
real_results = []
for sql in chosen:
    del events[:]
    optimized, planned, result = run(sql)
    check((optimized, planned) == (1, 1), "while recording every query is optimized and planned")
    check(events == ["X", "L", "F", "L", "X", "L"], f"the windows of one call, in order: {events}")
    real_results.append(result)
del events[:]
try:
    run("SELECT no_such_column FROM store_sales")
    raised = False
except Exception:
    raised = True
check(raised and events == ["X", "L", "F", "L"], f"an optimizer that raises closes its window: {events}")
runner.MEMO_PHASE = None
runner.MEMO_RECORD = None
records = []
for name in sorted(os.listdir(record_dir), key=lambda entry: int(entry.split("-")[1])):
    with open(os.path.join(record_dir, name), "rb") as handle:
        records.append(pickle.load(handle))
check(len(records) == 3 and all(len(record) == 4 for record in records), "three records of four parts")
check(all(type(record[3]) is str for record in records), "each with the front key of its optimize call")
check(len({record[3] for record in records}) == 3 and len({record[0] for record in records}) == 3, "three front keys, three memo keys")
runner.EXECUTOR_MEMO = {record[0]: record[2] for record in records}
runner.EXECUTOR_QUERIES = {record[0]: (1 << index, 0, record[1], {}) for index, record in enumerate(records)}
runner.EXECUTOR_FRONT = {record[3]: (record[0].rsplit(":", 1)[1], 1 << index) for index, record in enumerate(records)}

# ---- use ---------------------------------------------------------------------
runner.MEMO_BLOCK = None
runner.FRONT_BLOCK = None
optimized, planned, result = run(chosen[0])
check((optimized, planned) == (1, 0) and same(result, real_results[0]), "front key off: optimized, then found by the optimized tree")
runner.FRONT_BLOCK = 0
for index, sql in enumerate(chosen):
    optimized, planned, result = run(sql)
    check((optimized, planned) == (0, 0), f"front key on, nothing changed: query {index} is neither optimized nor planned")
    check(same(result, real_results[index]), f"and its recorded result comes back ({index})")
runner.FRONT_BLOCK = 1 << 0
optimized, planned, result = run(chosen[0])
check((optimized, planned) == (1, 0) and same(result, real_results[0]), "a changed line of its optimizer window: optimized for real")
optimized, planned, result = run(chosen[1])
check((optimized, planned) == (0, 0) and same(result, real_results[1]), "another query's window: not this one's business")
runner.FRONT_BLOCK = 0
runner.MEMO_BLOCK = (1 << 0, 0, {})
optimized, planned, result = run(chosen[0])
check((optimized, planned) == (1, 1) and same(result, real_results[0]), "a changed line of its planning and execution: everything for real")
optimized, planned, result = run(chosen[1])
check((optimized, planned) == (0, 0), "which leaves the other queries alone")
runner.MEMO_BLOCK = None

tree = tree_of(chosen[0])
names = [table.name for table in find_tables(tree)]
copies = {name: type(TABLES[name])(columns=tuple(TABLES[name].columns), rows=list(TABLES[name].rows)) for name in names}
optimized, planned, result = run(chosen[0], tables=copies)
check((optimized, planned) == (0, 0) and same(result, real_results[0]), "other table objects with the same rows: still known")
changed = dict(copies)
victim = names[0]
rows = list(TABLES[victim].rows)
rows[0] = tuple(None for _ in rows[0])
changed[victim] = type(TABLES[victim])(columns=tuple(TABLES[victim].columns), rows=rows)
optimized, planned, result = run(chosen[0], tables=changed)
check((optimized, planned) == (1, 1), "one changed row: optimized, planned and executed")
optimized, planned, result = run(chosen[0], schema=reordered)
check(optimized == 1, "another schema: optimized for real")


def note(tree):
    tree.expressions[0].comments = ["a note"]


optimized, planned, result = run(chosen[0], change=note)
check(optimized == 1, "a tree with another comment: optimized for real")


def spoil(tree):
    tree._hash = hash(tree) + 1


optimized, planned, result = run(chosen[0], change=spoil)
check(optimized == 1, "a tree with a wrong cached hash: optimized for real")
runner.MEMO_SCOPE = None
optimized, planned, result = run(chosen[0])
check((optimized, planned) == (1, 1) and same(result, real_results[0]), "outside the two tests nothing is taken from the record")
runner.MEMO_SCOPE = "tpcds"
runner.EXECUTOR_FRONT = {}
optimized, planned, result = run(chosen[0])
check((optimized, planned) == (1, 0), "a front key that was never recorded: optimized, then found as before")
runner.EXECUTOR_FRONT = {record[3]: (record[0].rsplit(":", 1)[1], 1 << index) for index, record in enumerate(records)}
runner.EXECUTOR_QUERIES = {}
optimized, planned, result = run(chosen[0])
check((optimized, planned) == (1, 1), "a result with no record of what it ran: everything for real")
runner.EXECUTOR_QUERIES = {record[0]: (1 << index, 0, record[1], {}) for index, record in enumerate(records)}

# The stand-in in the hands of an executor it was not made for.
tree = tree_of(chosen[0])
given = {table.name: TABLES[table.name] for table in find_tables(tree)}
made = executor.ensure_tables(given)
known = executor.optimize(tree, ensure_schema(SCHEMA), leave_tables_isolated=True, dialect=None)
check(type(known) is runner._Known and known.tables is made, "optimize hands back a stand-in for a known result")
check(executor.Plan(known) is known, "which the planner passes on")
other = executor.ensure_tables(given)
before = dict(calls)
result = executor.PythonExecutor(tables=other).execute(known)
check(same(result, real_results[0]), "an executor with other tables than the key was checked for gives the right result")
check(calls["optimize"] - before["optimize"] == 1, "by optimizing after all")
made = executor.ensure_tables(given)
known = executor.optimize(tree_of(chosen[0]), ensure_schema(SCHEMA), leave_tables_isolated=True, dialect=None)
before = dict(calls)
result = executor.PythonExecutor(tables=made).execute(executor.Plan(known))
check(type(known) is runner._Known and same(result, real_results[0]), "the executor it was made for hands back the record")
check(calls == before, "without optimizing or planning")
known = executor.optimize(tree_of(chosen[0]), ensure_schema(SCHEMA), leave_tables_isolated=True, dialect=None)
check(type(known) is not runner._Known, "a call that prepared no tables first gets no stand-in")

# The front file as the coordinator reads it.
import marshal  # noqa: E402
import zlib  # noqa: E402

path = os.path.join(record_dir, "execfrontq.bin")
with open(path, "wb") as handle:
    handle.write(zlib.compress(marshal.dumps({record[3]: (record[0].rsplit(":", 1)[1], (1 << index).to_bytes(1, "little")) for index, record in enumerate(records)})))
check(runner.read_executor_front(path) == runner.EXECUTOR_FRONT, "the map file reads back as written")
check(runner.read_executor_front(path + ".none") == {}, "a missing map is an empty one")
print(f"{checks} checks passed")
