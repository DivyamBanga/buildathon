"""Where does test_execute_tpcds spend its time? (development aid)"""
import os
import sys
import time

sys.path.insert(0, "/repo")
from selector import runner  # noqa: E402

os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up("/sqlglot", ["tests/test_executor.py"])
import tests.test_executor as module  # noqa: E402
import duckdb  # noqa: E402

spent = {"duckdb": 0.0, "calls": 0, "frame": 0.0}
real_connect = duckdb.connect


class Proxy:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        started = time.perf_counter()
        result = self._conn.execute(*args, **kwargs)
        frame = None
        if args and isinstance(args[0], str) and not args[0].lstrip().upper().startswith("CREATE"):
            frame = result.fetchdf()
            spent["calls"] += 1
        spent["duckdb"] += time.perf_counter() - started
        if frame is None:
            return result

        class Done:
            def fetchdf(self):
                return frame

        return Done()

    def __getattr__(self, name):
        return getattr(self._conn, name)


duckdb.connect = lambda *a, **k: Proxy(real_connect(*a, **k))
import pandas.testing  # noqa: E402

real_equal = module.assert_frame_equal


def timed_equal(*args, **kwargs):
    started = time.perf_counter()
    try:
        return real_equal(*args, **kwargs)
    finally:
        spent["frame"] += time.perf_counter() - started


module.assert_frame_equal = timed_equal
messages = []
started = time.perf_counter()
cpu0 = os.times()
runner.run_tests([(0, "tests/test_executor.py::TestExecutor::" + sys.argv[1])], messages.append)
cpu1 = os.times()
wall = time.perf_counter() - started
print(sys.argv[1], messages[-1].strip(), f"wall {wall:.1f}s",
      f"parent cpu {cpu1.user + cpu1.system - cpu0.user - cpu0.system:.1f}s",
      f"children cpu {cpu1.children_user + cpu1.children_system - cpu0.children_user - cpu0.children_system:.1f}s",
      f"duckdb wall {spent['duckdb']:.1f}s over {spent['calls']} queries, frame compare {spent['frame']:.1f}s")
