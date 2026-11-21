"""Record what DuckDB answers for the queries the executor tests send it.

    python build_duckdb_answers.py /out/maps/duckdb.bin

The executor tests compare sqlglot's own executor with DuckDB running the same
query over the TPC-H and TPC-DS tables. DuckDB's side depends only on the SQL
text it receives, and costs more than half of those tests' CPU time, so it is
recorded here once, at the pinned commit, keyed by that exact text.
"""
import os
import pickle
import sys
import zlib

sys.path.insert(0, "/repo")
from selector import runner  # noqa: E402

os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up("/sqlglot", ["tests/test_executor.py"])
import tests.test_executor as module  # noqa: E402

recorded = {"tpch_conn": {}, "tpcds_conn": {}}


class Recorder:
    def __init__(self, connection, answers):
        self._connection = connection
        self._answers = answers

    def execute(self, query, *args, **kwargs):
        result = self._connection.execute(query, *args, **kwargs)
        if args or kwargs or type(query) is not str:
            return result
        frame = result.fetchdf()
        self._answers[query] = pickle.dumps(frame, protocol=pickle.HIGHEST_PROTOCOL)

        class Done:
            def fetchdf(self):
                return frame.copy()

            df = fetchdf

        return Done()

    def __getattr__(self, name):
        return getattr(self._connection, name)


test_class = module.TestExecutor
original = test_class.__dict__["setUpClass"].__func__


def setUpClass(cls):
    original(cls)
    for name, answers in recorded.items():
        setattr(cls, name, Recorder(getattr(cls, name), answers))


test_class.setUpClass = classmethod(setUpClass)
ids = [f"tests/test_executor.py::TestExecutor::{name}" for name in sorted(dir(test_class)) if name.startswith("test")]
messages = []
runner.run_tests(list(enumerate(ids)), messages.append)
failed = [message for message in messages if message.startswith("R") and message.endswith(" 1\n")]
if failed:
    raise SystemExit(f"tests failed while recording: {failed}")
with open(sys.argv[1], "wb") as handle:
    handle.write(zlib.compress(pickle.dumps(recorded, protocol=pickle.HIGHEST_PROTOCOL), 9))
print({name: len(answers) for name, answers in recorded.items()}, os.path.getsize(sys.argv[1]), "bytes")
