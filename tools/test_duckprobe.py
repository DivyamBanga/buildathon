"""Unit checks for runner._ProbingConnection (the stand-in for a DuckDB connection).

    PYTHONPATH=/repo python tools/test_duckprobe.py

A fake connection records what really reaches DuckDB, so every check states
both what the caller sees and what DuckDB was sent, in order.
"""
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from selector import runner  # noqa: E402

MARK = runner.ROOT_MARK
SETUP = (f"CREATE VIEW a AS SELECT * FROM READ_CSV('{MARK}/tests/fixtures/a.csv.gz')", "CREATE TABLE b (x INT)")
ANSWERS = {"SELECT 1": pickle.dumps("one"), "SELECT x FROM b": pickle.dumps("rows of b")}
PINNED = "/sqlglot/"
OVERLAY = "/tmp/br/c1-1/"


class Fake:
    def __init__(self, log):
        self.log = log
        self.closed = False

    def execute(self, query, *args, **kwargs):
        if query == "BOOM":
            raise ValueError("boom")
        self.log.append((query, args, kwargs))
        return self

    def fetchall(self):
        return [("real",)]

    def close(self):
        self.closed = True

    def register(self, name, value):
        self.log.append(("register", name))


def make(setups=None):
    log = []
    opened = []

    def connect():
        opened.append(Fake(log))
        return opened[-1]

    table = {SETUP: ANSWERS} if setups is None else setups
    return runner._ProbingConnection(connect, table, [OVERLAY, PINNED]), log, opened


def send_setup(connection, root):
    for statement in SETUP:
        connection.execute(statement.replace(MARK + "/", root))


checks = 0


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        raise SystemExit("FAILED: " + message)


# 1. The pinned setup, seen under the pinned path: nothing is opened, known queries are answered.
c, log, opened = make()
send_setup(c, PINNED)
check(not opened and not log, "the setup alone must not open DuckDB")
check(c.execute("SELECT 1").fetchdf() == "one", "a recorded query gets its recorded answer")
check(c.execute("SELECT x FROM b").df() == "rows of b", "df() is fetchdf()")
check(not opened, "answering must not open DuckDB")
c.close()
check(not opened, "closing a connection that was never needed opens nothing")

# 2. The same setup seen through an overlay path is the same setup.
c, log, opened = make()
send_setup(c, OVERLAY)
check(c.execute("SELECT 1").fetchdf() == "one" and not opened, "overlay paths are recognised")

# 3. An unknown SELECT opens DuckDB, replays the setup in order, and answering goes on.
c, log, opened = make()
send_setup(c, OVERLAY)
result = c.execute("SELECT 2")
check(len(opened) == 1, "an unknown query needs the real connection")
check([entry[0] for entry in log] == [s.replace(MARK + "/", OVERLAY) for s in SETUP] + ["SELECT 2"],
      f"the setup must reach DuckDB first, as it was given: {log}")
check(result.fetchall() == [("real",)], "the unknown query's result is DuckDB's")
check(c.execute("SELECT 1").fetchdf() == "one" and len(log) == 3, "after a plain SELECT recorded answers still apply")

# 4. A statement that may change the connection ends answering for good.
c, log, opened = make()
send_setup(c, PINNED)
c.execute("INSERT INTO b VALUES (1)")
c.execute("SELECT x FROM b")
check([entry[0] for entry in log][-2:] == ["INSERT INTO b VALUES (1)", "SELECT x FROM b"],
      "after a write, a recorded query is really executed")

# 5. A different first statement: plain pass-through from the start.
c, log, opened = make()
c.execute("CREATE TABLE other (y INT)")
check(len(opened) == 1 and [entry[0] for entry in log] == ["CREATE TABLE other (y INT)"], "an unknown setup is passed on at once")
c.execute("SELECT 1")
check([entry[0] for entry in log][-1] == "SELECT 1", "nothing is answered on an unrecognised connection")

# 6. The setup's first statement, then something else: what was held is sent first.
c, log, opened = make()
first = SETUP[0].replace(MARK + "/", PINNED)
c.execute(first)
check(not opened, "a prefix of the setup is held back")
c.execute("CREATE TABLE b (x TEXT)")
check([entry[0] for entry in log] == [first, "CREATE TABLE b (x TEXT)"], "the held statement goes first")
c.execute("SELECT 1")
check([entry[0] for entry in log][-1] == "SELECT 1", "a setup that deviated is never answered from the record")

# 7. One statement too many after the setup, even a repeated one, is a write: no more answers.
c, log, opened = make()
send_setup(c, PINNED)
c.execute(SETUP[1])
c.execute("SELECT 1")
check([entry[0] for entry in log][-1] == "SELECT 1", "an extra setup statement ends answering")

# 8. Parameters, or any other use of the connection, end answering.
c, log, opened = make()
send_setup(c, PINNED)
c.execute("SELECT ?", [1])
c.execute("SELECT 1")
check([entry[0] for entry in log][-2:] == ["SELECT ?", "SELECT 1"], "a parametrised call ends answering")
c, log, opened = make()
send_setup(c, PINNED)
c.register("t", object())
c.execute("SELECT 1")
check(log[-2:] == [("register", "t"), ("SELECT 1", (), {})], "another method ends answering")

# 9. Asking an answered query for more than its frame runs it for real.
c, log, opened = make()
send_setup(c, PINNED)
check(c.execute("SELECT 1").fetchall() == [("real",)], "fetchall() of an answered query is DuckDB's")
check([entry[0] for entry in log][-1] == "SELECT 1", "and the query was really sent")

# 10. An error from DuckDB is the caller's error, and the connection stays usable.
c, log, opened = make()
send_setup(c, PINNED)
try:
    c.execute("BOOM")
    raise SystemExit("FAILED: the error must surface")
except ValueError:
    checks += 1
c.execute("SELECT 1")
check([entry[0] for entry in log][-1] == "SELECT 1", "after a failed unknown statement nothing is answered")

# 11. With-statement and close.
c, log, opened = make()
with c as inner:
    check(inner is c, "the context manager yields the stand-in")
    send_setup(c, PINNED)
    c.execute("SELECT 2")
check(opened[0].closed, "leaving the block closes the real connection")

# 12. A setup with no recorded answers is only held back.
c, log, opened = make({SETUP: {}})
send_setup(c, PINNED)
check(not opened, "held back")
c.execute("SELECT 1")
check([entry[0] for entry in log] == [s.replace(MARK + "/", PINNED) for s in SETUP] + ["SELECT 1"], "then replayed")

# 13. What counts as a plain read.
for query, expected in (
    ("SELECT 1", True), ("  select * from t ;", True), ("WITH a AS (SELECT 1) SELECT * FROM a", True),
    ("(SELECT 1) UNION ALL (SELECT 2)", True), ("SELECT 1; DROP VIEW a", False), ("CREATE VIEW v AS SELECT 1", False),
    ("INSERT INTO b VALUES (1)", False), ("SELECT nextval('s')", False), ("SELECT setseed(0.1)", False),
    ("WITH a AS (SELECT 1) INSERT INTO b SELECT * FROM a", False), ("PRAGMA threads=1", False), ("", False),
):
    check(runner._only_reads(query) is expected, f"_only_reads({query!r}) should be {expected}")

# 14. A query with no recorded answer over views of CSV files: answered from tables when that can be vouched for.
try:
    import duckdb
except ImportError:
    duckdb = None
if duckdb is not None:
    import gzip
    import tempfile

    from pandas.testing import assert_frame_equal

    folder = tempfile.mkdtemp()
    rows = [(i, i % 3, round(i * 0.1, 1)) for i in range(1, 61)]
    with gzip.open(os.path.join(folder, "t.csv.gz"), "wt", newline="") as handle:
        handle.write("k|g|v\n" + "".join(f"{k}|{g}|{v}\n" for k, g, v in rows))
    with gzip.open(os.path.join(folder, "u.csv.gz"), "wt", newline="") as handle:
        handle.write("g|name\n0|zero\n1|one\n2|two\n")
    views = (
        f"\n   CREATE VIEW t AS\n   SELECT *\n   FROM READ_CSV('{MARK}/t.csv.gz', delim='|', header=True, "
        "columns={'k': 'bigint', 'g': 'bigint', 'v': 'double'})\n  ",
        f"CREATE VIEW u AS SELECT * FROM READ_CSV('{MARK}/u.csv.gz', delim='|', header=True, "
        "columns={'g': 'bigint', 'name': 'string'})",
    )
    recorded = {"SELECT 1 AS one": pickle.dumps("recorded")}
    real_log = []

    class Logged:
        """A real connection; what is sent to one that holds the views is noted."""

        def __init__(self):
            self.inner = duckdb.connect()
            self.views = False

        def execute(self, query, *args, **kwargs):
            self.views = self.views or "CREATE VIEW" in query
            if self.views:
                real_log.append(query)
            return self.inner.execute(query, *args, **kwargs)

        def cursor(self):
            return self.inner.cursor()

        def close(self):
            self.inner.close()

    def real_views():
        connection = duckdb.connect()
        for statement in views:
            connection.execute(statement.replace(MARK + "/", folder + "/"))
        return connection

    def stand_in():
        del real_log[:]
        runner._TABLE_COPIES.clear()
        connection = runner._ProbingConnection(Logged, {views: recorded}, [folder + "/"])
        for statement in views:
            connection.execute(statement.replace(MARK + "/", folder + "/"))
        return connection

    truth = real_views()
    c = stand_in()
    check(c.execute("SELECT 1 AS one").fetchdf() == "recorded", "a recorded answer is still given as recorded")
    determined = "SELECT u.name, SUM(t.v) AS total, COUNT(*) AS n FROM t JOIN u ON t.g = u.g GROUP BY u.name ORDER BY u.name"
    frame = c.execute(determined).fetchdf()
    check(not real_log, f"a fully ordered query is answered without the views: {real_log}")
    assert_frame_equal(frame, truth.execute(determined).fetchdf(), check_exact=False, rtol=1e-9)
    checks += 1
    frame.rename(columns={"total": "x"}, inplace=True)
    check("total" in c.execute(determined).fetchdf().columns, "every caller gets a frame of its own")
    for open_ended, why in (
        ("SELECT k FROM t", "no order at all"),
        ("SELECT k, g FROM t ORDER BY g LIMIT 7", "ties under the ORDER BY decide what LIMIT keeps"),
        ("SELECT g, FIRST(k) AS k FROM t GROUP BY g ORDER BY g", "an aggregate that depends on arrival order"),
    ):
        del real_log[:]
        frame = c.execute(open_ended).fetchdf()
        check(open_ended in real_log, f"the views are asked when the result is not fixed by the query ({why})")
        assert_frame_equal(frame, truth.execute(open_ended).fetchdf())
        checks += 1
    del real_log[:]
    for telltale in ("SELECT rowid FROM t ORDER BY 1", "SELECT COUNT(*) AS n FROM main.t"):
        try:
            c.execute(telltale).fetchdf()
        except Exception:
            pass
        check(telltale in real_log, f"a query that could tell a table from a view goes to the views: {telltale}")
    del real_log[:]
    try:
        c.execute("SELECT nothing FROM t ORDER BY 1")
        check(False, "an invalid query must raise")
    except Exception as error:
        check("nothing" in str(error) and "SELECT nothing FROM t ORDER BY 1" in real_log, "an error is the views' own error")
    c = stand_in()
    c.execute("CREATE TABLE extra AS SELECT 1 AS k")
    del real_log[:]
    c.execute(determined).fetchdf()
    check(determined in real_log, "after a statement that changes the connection the views are asked")
    check(
        runner._frame_from_tables(Logged, ("CREATE TABLE b (x INT)",), "SELECT 1 AS k") is None,
        "a setup of anything but CSV views has no table copies",
    )

print(f"{checks} checks passed")
