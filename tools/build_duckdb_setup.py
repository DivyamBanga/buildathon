"""Record the statements the tests' class setups send to DuckDB.

    python build_duckdb_setup.py /out/maps/ducksetup.json

What a DuckDB connection holds is decided by the statements it was sent. Two
test classes open connections in their class setup and prepare them the same
way every time: the executor tests (one CREATE VIEW per TPC table, reading a
CSV file) and the optimizer tests (four small tables). The statements are
recorded here per connection, with the checkout's path written as <ROOT>, so
that a child can recognise a connection its own setup prepares in exactly that
way (see runner._ProbingConnection): such a connection need not be opened
until DuckDB is really asked something, and for the executor tests the answers
in maps/duckdb.bin apply to it.
"""
import json
import os
import re
import sys

sys.path.insert(0, "/repo")
from selector import runner  # noqa: E402

ROOT = "/sqlglot"
CLASSES = (("tests.test_executor", "TestExecutor"), ("tests.test_optimizer", "TestOptimizer"))
os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up(ROOT, ["tests/test_executor.py", "tests/test_optimizer.py"])
import duckdb  # noqa: E402

real_connect = duckdb.connect


class Recorder:
    def __init__(self, connection):
        self._connection = connection
        self.statements = []
        self.other = []

    def execute(self, query, *args, **kwargs):
        if args or kwargs or type(query) is not str:
            self.other.append("execute with parameters")
        else:
            self.statements.append(query.replace(ROOT + "/", runner.ROOT_MARK + "/"))
        return self._connection.execute(query, *args, **kwargs)

    def __getattr__(self, name):
        self.other.append(name)
        return getattr(self._connection, name)


made = []


def connect(*args, **kwargs):
    recorder = Recorder(real_connect(*args, **kwargs))
    if args or kwargs:
        recorder.other.append("connect with arguments")
    made.append(recorder)
    return recorder


connections = {}
for module_name, class_name in CLASSES:
    test_class = getattr(sys.modules[module_name], class_name)
    duckdb.connect = connect
    try:
        test_class.setUpClass()
    finally:
        duckdb.connect = real_connect
    for name, value in vars(test_class).items():
        if isinstance(value, Recorder):
            if value.other:
                raise SystemExit(f"{class_name}.{name}: the setup did more than execute statements: {value.other}")
            if name in connections:
                raise SystemExit(f"two connections named {name}")
            text = "\n".join(value.statements)
            files = sorted(set(re.findall(re.escape(runner.ROOT_MARK) + r"/([^'\"\s]+)", text)))
            connections[name] = {"class": class_name, "statements": value.statements, "files": files}
    test_class.doClassCleanups()
for recorder in made:
    recorder._connection.close()
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({"connections": connections}, handle, separators=(",", ":"), sort_keys=True)
print({name: (entry["class"], len(entry["statements"]), len(entry["files"])) for name, entry in connections.items()})
