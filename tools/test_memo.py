"""Checks of the executor memo's key and of its table check (run in the lab image).

    PYTHONPATH=/sqlglot:/repo python /tools/test_memo.py
"""
import os
import sys

sys.path.insert(0, "/repo")
os.environ["SKIP_INTEGRATION"] = "0"
from selector import runner  # noqa: E402

from sqlglot import exp, parse_one  # noqa: E402
from sqlglot.executor.table import Table, Tables  # noqa: E402
from sqlglot.optimizer import optimize  # noqa: E402

checks = 0


def check(condition, what):
    global checks
    checks += 1
    if not condition:
        raise SystemExit(f"FAILED: {what}")


digest = runner.tree_digest
sql = "SELECT a, SUM(b) AS s FROM t WHERE c = 'x' AND d > 1.5 GROUP BY a ORDER BY s DESC LIMIT 3"
one, two = parse_one(sql), parse_one(sql)
check(digest(one) == digest(two), "the same query parsed twice has one digest")
check(digest(one) == digest(one.copy()), "a copy has the digest of its original")
check(digest(one) != digest(parse_one(sql.replace("1.5", "1.50"))), "another literal text is another digest")
check(digest(one) != digest(parse_one(sql.replace("DESC", "ASC"))), "another ordering is another digest")
check(digest(parse_one("SELECT 1")) != digest(parse_one("SELECT '1'")), "a number is not a string")
schema = {"t": {"a": "INT", "b": "INT", "c": "TEXT", "d": "DOUBLE"}}
check(digest(optimize(one.copy(), schema)) == digest(optimize(two.copy(), schema)), "optimizing twice gives one digest")
check(digest(optimize(one.copy(), schema)) != digest(one), "the optimized tree differs from the parsed one")

# Leaves: type and value both count.
a, b = exp.Literal(this="1", is_string=False), exp.Literal(this="1", is_string=True)
check(digest(a) != digest(b), "True and False differ")
a, b = exp.Limit(expression=1), exp.Limit(expression=True)
check(digest(a) != digest(b), "1 and True differ")
a, b = exp.Limit(expression=1), exp.Limit(expression=1.0)
check(digest(a) != digest(b), "1 and 1.0 differ")
a, b = exp.Limit(expression=1), exp.Limit(expression="1")
check(digest(a) != digest(b), "1 and '1' differ")

# Absent, None and an empty list are three things.
plain = parse_one("SELECT a FROM t")
none = plain.copy()
none.args["joins"] = None
empty = plain.copy()
empty.args["joins"] = []
check(len({digest(plain), digest(none), digest(empty)}) == 3, "absent, None and [] are told apart")

# The order of the arguments counts.
swapped = plain.copy()
swapped.args = dict(reversed(list(swapped.args.items())))
check(digest(swapped) != digest(plain), "argument order counts")

# Type, comments and meta count.
typed = plain.copy()
typed.expressions[0].type = exp.DataType.build("int")
check(digest(typed) != digest(plain), "a type annotation counts")
both = runner.tree_digests
check(runner.SLOTS == ("_type", "comments", "_meta"), "the three slots, in the order of the digests")
check(both(typed)[0] == both(plain)[0], "but not in the key")
check(both(typed)[1][0] != both(plain)[1][0] and both(typed)[1][1:] == both(plain)[1][1:], "an annotation changes its own digest only")
retyped = plain.copy()
retyped.expressions[0].type = exp.DataType.build("text")
check(both(retyped)[0] == both(typed)[0] and both(retyped)[1][0] != both(typed)[1][0], "another annotation changes only the annotation digest")
moved = plain.copy()
moved.args["from_"].type = exp.DataType.build("int")
check(both(moved)[1][0] != both(typed)[1][0], "the same annotation on another node is another digest")
check(both(plain.copy()) == both(plain), "a copy keeps them all")
noted = plain.copy()
noted.expressions[0].comments = ["a note"]
check(both(noted)[0] == both(plain)[0] and both(noted)[1][1] != both(plain)[1][1], "comments: out of the key, in their digest")
check(both(noted)[1][0] == both(plain)[1][0] and both(noted)[1][2] == both(plain)[1][2], "and in no other")
placed = plain.copy()
placed.expressions[0].meta["line"] = 3
check(both(placed)[0] == both(plain)[0] and both(placed)[1][2] != both(plain)[1][2], "meta: out of the key, in its digest")
elsewhere = plain.copy()
elsewhere.expressions[0].meta["line"] = 4
check(both(elsewhere)[1][2] != both(placed)[1][2], "another position is another meta digest")
odd = plain.copy()
odd.expressions[0].comments = [object]
check(both(odd)[0] != both(plain)[0], "comments that are not a list of strings stay in the key")
odd = plain.copy()
odd.expressions[0].comments = ("a note",)
check(both(odd)[0] != both(plain)[0], "comments in a tuple stay in the key")
odd = plain.copy()
odd.expressions[0].meta["node"] = exp.column("x")
check(both(odd)[0] != both(plain)[0], "a meta dictionary that holds more than plain values stays in the key")
odd = plain.copy()
odd.expressions[0]._type = odd.expressions[0]
check(both(odd)[0] != both(plain)[0], "a node annotated with itself is told apart by the key")
odd = plain.copy()
odd.expressions[0]._type = object
check(both(odd)[0] != both(plain)[0], "an annotation that is not an expression stays in the key")
looped = plain.copy()
looped.expressions[0].meta["self"] = [looped.expressions[0]]
try:
    both(looped)
    check(False, "a slot that leads back to its node must be refused")
except ValueError:
    checks += 1
commented = plain.copy()
commented.expressions[0].comments = ["x"]
check(digest(commented) != digest(plain), "a comment counts")
meta = plain.copy()
meta.expressions[0].meta["k"] = 1
check(digest(meta) != digest(plain), "meta counts")

# Wiring: one node in two places, or a child that names another parent or position.
shared = parse_one("SELECT a, a FROM t")
healthy = digest(shared)
column = shared.expressions[0]
shared.args["expressions"][1] = column
check(digest(shared) != healthy, "a node sitting in two places is told apart from two equal nodes")
stale = parse_one("SELECT a, b FROM t")
healthy = digest(stale)
stale.expressions[1].index = 0
check(digest(stale) != healthy, "a stale index counts")
stale = parse_one("SELECT a, b FROM t")
stale.expressions[1].arg_key = "this"
check(digest(stale) != healthy, "a stale argument name counts")
stale = parse_one("SELECT a, b FROM t")
stale.expressions[1].parent = stale.expressions[0]
check(digest(stale) != healthy, "a stale parent counts")

# A tree that hangs under another node is refused: what is above it is not in the digest.
try:
    digest(parse_one("SELECT a FROM t").expressions[0])
    check(False, "a subtree is refused")
except ValueError:
    check(True, "a subtree is refused")

# A deep tree does not hit the recursion limit.
deep = parse_one("SELECT " + " + ".join(["x"] * 3000))
check(len(digest(deep)) == 40, "a deep tree is digested")

# The tables handed to the executor must be the pinned ones, by name and by content.
pinned = {"t": Table(("a", "b"), [(1, "x"), (2, None)]), "u": Table(("c",), [(1.5,)])}
runner.EXECUTOR_TABLES = ({}, pinned)
runner._snapshot_pinned()
key = runner.memo_key("tpcds", "d", (), {"tables": Tables({"t": pinned["t"]})})
check(key == "tpcds:t:d", f"the pinned table itself is recognised ({key})")
same = Table(("a", "b"), [(1, "x"), (2, None)])
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"t": same, "u": pinned["u"]})}) == "tpcds:t,u:d", "an equal table is recognised")
for label, table in (
    ("another value", Table(("a", "b"), [(1, "x"), (3, None)])),
    ("another value type", Table(("a", "b"), [(1.0, "x"), (2, None)])),
    ("a boolean for 1", Table(("a", "b"), [(True, "x"), (2, None)])),
    ("another column name", Table(("a", "z"), [(1, "x"), (2, None)])),
    ("a row less", Table(("a", "b"), [(1, "x")])),
    ("lists for rows", Table(("a", "b"), [[1, "x"], [2, None]])),
    ("a column range", Table(("a", "b"), [(1, "x"), (2, None)], column_range=range(0, 1))),
):
    check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"t": table})}) is None, f"not recognised: {label}")
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"x": pinned["t"]})}) is None, "an unknown name is not recognised")
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"u": pinned["t"]})}) is None, "a table under another's name is not recognised")
check(runner.memo_key("tpcds", "d", (), {"tables": {"t": pinned["t"]}}) is None, "a plain dict is not what ensure_tables returns")
check(runner.memo_key("tpcds", "d", (pinned,), {}) is None, "positional arguments are not what execute passes")
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"t": pinned["t"]}), "env": {}}) is None, "another argument is not what execute passes")
check(runner.memo_key("tpch", "d", (), {"tables": Tables({"t": pinned["t"]})}) is None, "the other query set has other tables")
# A table changed in place after it was built or compared is no longer the pinned data.
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"t": same})}) == "tpcds:t:d", "an equal table is recognised again")
same.rows[0] = (1, "y")
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"t": same})}) is None, "not recognised: a row replaced after the comparison")
again = Table(("a", "b"), [(1, "x"), (2, None)])
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"t": again})}) == "tpcds:t:d", "another equal table is recognised")
again.rows.reverse()
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"t": again})}) is None, "not recognised: rows reordered in place")
again.rows.reverse()
again.reader.columns["b"] = 0
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"t": again})}) is None, "not recognised: a reader that maps columns otherwise")
kept = list(pinned["u"].rows)
pinned["u"].rows.clear()
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"u": pinned["u"]})}) is None, "not recognised: the pinned table itself, emptied in place")
pinned["u"].rows.extend(kept)
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"u": pinned["u"]})}) == "tpcds:u:d", "recognised again with its own rows back")
pinned["u"].rows[0] = tuple([1.5])  # equal to the row it replaces, but another object
check(runner.memo_key("tpcds", "d", (), {"tables": Tables({"u": pinned["u"]})}) is None, "not recognised: an equal row that is another object in the pinned table")
pinned["u"].rows[0] = kept[0]
nested = Tables({"db": {"t": pinned["t"]}})
check(runner.memo_key("tpcds", "d", (), {"tables": nested}) is None, "nested tables are not recognised")
# Cached hashes: a recorded result is not for a tree in which one of them is out of date.
hashed = parse_one("SELECT a FROM t WHERE a = 1")
hash(hashed)
check(runner.hashes_fresh(hashed), "a tree whose cached hashes are the true ones")
equality = hashed.args["where"].this
cached = equality._hash
check(cached is not None, "the comparison has a cached hash")
equality.this.args["this"] = exp.to_identifier("b")  # a change below it that clears no cache
check(not runner.hashes_fresh(hashed), "a cached hash that predates a change below it is found")
check(equality._hash == cached and hashed._hash is not None, "and every cache is left as it was")
bare = parse_one("SELECT a FROM t")
check(runner.hashes_fresh(bare) and bare._hash is None and bare.expressions[0]._hash is None, "a tree with no cached hash is fresh and stays without one")
typed_node = parse_one("SELECT a FROM t")
typed_node.expressions[0].type = exp.DataType.build("array<int>")
hash(typed_node.expressions[0].type)
check(runner.hashes_fresh(typed_node), "annotations are looked at as well")
typed_node.expressions[0].type.expressions[0].args["this"] = exp.DataType.Type.TEXT
check(not runner.hashes_fresh(typed_node), "and a stale hash inside one is found")
# Query by query: a recorded result is used only when the query's own record touches nothing the patch changes.
runner.EXECUTOR_QUERIES = {
    "k": (0b0110, 0b01, ("", "", ""), {}),
    "t": (0b0110, 0b01, ("typed", "", ""), {}),
    "c": (1, 1, ("", "noted", ""), {}),
    # reads attribute 0 whole and attribute 2 by two keys only
    "d": (0b0001, 0b101, ("", "", ""), {2: frozenset(("str:'a'", "num:7", "int:7"))}),
}
runner.MEMO_BLOCK = None
check(runner._query_untouched("k", ("x", "y", "z")), "nothing changed in the phase: a query that reads no slot keeps its result")
check(runner._query_untouched("t", ("typed", "y", "z")), "a query that asks for types keeps its result with the recorded annotations")
check(not runner._query_untouched("t", ("other", "y", "z")), "and is executed with other annotations")
check(runner._query_untouched("c", ("x", "noted", "z")) and not runner._query_untouched("c", ("x", "y", "z")), "the same for comments")
check(not runner._query_untouched("unknown", ("typed", "", "")), "a query with no record of its own is executed")
for block, expected in (
    ((0b1000, 0, {}), True), ((0b0100, 0, {}), False), ((0, 0b01, {}), False), ((0b1001, 0b10, {}), True), ((0b0010, 0b10, {}), False),
):
    runner.MEMO_BLOCK = block
    check(runner._query_untouched("k", ("x", "y", "z")) is expected, f"lines and attributes {block}: kept is {expected}")
for block, expected, why in (
    ((0, 0, {2: frozenset(("str:'b'",))}), True, "a table read by key, another entry differs"),
    ((0, 0, {2: frozenset(("str:'a'",))}), False, "an entry that was looked up differs"),
    ((0, 0, {2: frozenset(("enum:m:E:SEVEN", "num:7"))}), False, "an entry under an equal key differs"),
    ((0, 0b100, {}), False, "the table differs as a whole"),
    ((0, 0, {0: frozenset(("str:'b'",))}), False, "a table this query walked differs in an entry"),
    ((0, 0, {1: frozenset(("str:'a'",))}), True, "a table this query never read differs"),
):
    runner.MEMO_BLOCK = block
    check(runner._query_untouched("d", ("x", "y", "z")) is expected, f"{why}: kept is {expected}")
runner.MEMO_BLOCK = None
queries = runner.read_executor_queries("/repo/maps/execqueries.bin")
check(
    len(queries) == 57 and all(type(a) is int and type(b) is int and len(c) == 3 and type(d) is dict and a for a, b, c, d in queries.values()),
    "the per-query record reads back",
)
asking = [sum(1 for entry in queries.values() if entry[2][position]) for position in range(3)]
check(0 < asking[0] < 57 and 0 < asking[1] < 57 and asking[2] < 57, f"only some queries read a slot: {asking}")
check(runner.read_executor_queries("/repo/maps/no-such-file") == {}, "a missing per-query record is an empty one")

# Which class attributes can be watched for reads (state.watchable), and how a pair is looked up.
from selector import state  # noqa: E402
from sqlglot import generator  # noqa: E402

world = {name: module for name, module in sys.modules.items() if name == "sqlglot" or name.startswith("sqlglot.")}
for module, qualname, name, expected in (
    ("sqlglot.expressions.query", "Select", "arg_types", True),
    ("sqlglot.generator", "Generator", "TRANSFORMS", True),
    ("sqlglot.generator", "Generator", "TYPE_MAPPING", True),
    ("sqlglot.generator", "Generator", "generate", False),  # a method: used by running it
    ("sqlglot.generator", "Generator", "__init__", False),
    ("sqlglot.generator", "Generator", "__slots__", False),  # a dunder
    ("sqlglot.generator", "Generator", "NO_SUCH_ATTRIBUTE", False),
    ("sqlglot.generator", "NoSuchClass", "TRANSFORMS", False),
    ("sqlglot.expressions.core", "Expr", "this", False),  # a property
    ("sqlglot.expressions.core", "Expr", "args", False),  # a slot
    ("sqlglot.tokens", "TokenType", "SELECT", False),  # an enum member
    ("sqlglot.executor.python", "Python.Tokenizer", "STRING_ESCAPES", True),  # a nested class
    ("sqlglot.executor.python", "Python", "Generator", False),  # a class held as an attribute
):
    found = state.watchable_pair(world, module, qualname, name)
    check(found is expected, f"watchable {module}:{qualname}|{name} should be {expected}")
check(state.watchable(generator.Generator, "X", {"a": 1}), "a dict is plain data")
check(not state.watchable(generator.Generator, "X", lambda: 1), "a function is not")
check(not state.watchable(generator.Generator, "X", staticmethod(len)), "a staticmethod is not")
check(not state.watchable(generator.Generator, "X", property(len)), "a property is not")
print(f"{checks} checks passed")
