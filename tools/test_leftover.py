"""Checks for the class tables that running code changes and puts back (selector/runner.py, RESTORED_TABLES).

    python tools/test_leftover.py [PINNED_SQLGLOT_DIR]

Needs the pinned tree (the argument, or /sqlglot): it parses with the pinned parser.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = sys.argv[1] if len(sys.argv) > 1 else "/sqlglot"
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
import unittest  # noqa: E402,F401  (sqlglot looks, when imported, whether tests are loaded)

from selector import runner  # noqa: E402

checks = 0


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        raise SystemExit(f"FAIL {label}: got {got!r}, want {want!r}")


from sqlglot import parse_one  # noqa: E402
from sqlglot.errors import ParseError  # noqa: E402
from sqlglot.parser import Parser  # noqa: E402

BASE = "sqlglot.parser.Parser.NO_PAREN_FUNCTION_PARSERS"
expect("before anything ran", runner.tables_left_changed(), [])
expect("the table has no PRIOR of its own", "PRIOR" in Parser.NO_PAREN_FUNCTION_PARSERS, False)
expect("PRIOR is a name like any other", parse_one("PRIOR AS x").sql(), "PRIOR AS x")
expect("a CONNECT BY that parses puts the table back", parse_one("SELECT * FROM t CONNECT BY PRIOR a = b").sql(), "SELECT * FROM t CONNECT BY PRIOR a = b")
expect("and nothing is left", runner.tables_left_changed(), [])

# the leak: the condition raises, the entry stays, and a later parse of another statement fails
try:
    parse_one("SELECT * FROM t CONNECT BY a = (")
    raised = False
except ParseError:
    raised = True
expect("a CONNECT BY condition that does not parse raises", raised, True)
expect("PRIOR stays in the class table", "PRIOR" in Parser.NO_PAREN_FUNCTION_PARSERS, True)
try:
    parse_one("PRIOR AS x")
    later = "parses"
except ParseError:
    later = "raises"
expect("so a later statement no longer parses", later, "raises")
expect("and the runner says which table", runner.tables_left_changed(), [BASE])

# a dialect that shares the base table is the same table, named once
from sqlglot.parsers.snowflake import SnowflakeParser  # noqa: E402

expect("snowflake has no table of its own", SnowflakeParser.NO_PAREN_FUNCTION_PARSERS is Parser.NO_PAREN_FUNCTION_PARSERS, True)
expect("one name for one table", runner.tables_left_changed(), [BASE])
Parser.NO_PAREN_FUNCTION_PARSERS.pop("PRIOR")
expect("put back by hand: nothing is left", runner.tables_left_changed(), [])

# a dialect with a table of its own, loaded after the first look and already holding a leftover
from sqlglot.parsers.duckdb import DuckDBParser  # noqa: E402

expect("duckdb has a table of its own", DuckDBParser.NO_PAREN_FUNCTION_PARSERS is Parser.NO_PAREN_FUNCTION_PARSERS, False)
try:
    parse_one("SELECT * FROM t CONNECT BY a = (", read="duckdb")
except ParseError:
    pass
# (the class was imported in this test as well: the first look at its table comes now)
left = runner.tables_left_changed()
expect("a leftover in a table first seen with it", [name for name in left if "DuckDBParser" in name] != [], True)
DuckDBParser.NO_PAREN_FUNCTION_PARSERS.pop("PRIOR")
expect("put back: nothing is left", runner.tables_left_changed(), [])

# Oracle keeps PRIOR for good and does not use the inserting function
from sqlglot.parsers.oracle import OracleParser  # noqa: E402

expect("oracle has PRIOR of its own", "PRIOR" in OracleParser.NO_PAREN_FUNCTION_PARSERS, True)
parse_one("SELECT * FROM t CONNECT BY PRIOR a = b", read="oracle")
expect("oracle's entry is not a leftover", runner.tables_left_changed(), [])
try:
    parse_one("SELECT * FROM t CONNECT BY a = (", read="oracle")
except ParseError:
    pass
expect("nor after a condition that raises", runner.tables_left_changed(), [])

# any other change of a reviewed table counts as well
Parser.NO_PAREN_FUNCTION_PARSERS["ZZ_LEFT"] = None
expect("another entry added", runner.tables_left_changed(), [BASE])
del Parser.NO_PAREN_FUNCTION_PARSERS["ZZ_LEFT"]
expect("and removed", runner.tables_left_changed(), [])

# no function of the pinned package other than the reviewed one stores into a class table and takes it out again
expect("the reviewed list", runner.RESTORED_TABLES, (("sqlglot.parser", "Parser", "NO_PAREN_FUNCTION_PARSERS", "_parse_connect_with_prior"),))
print(f"{checks} checks passed")
