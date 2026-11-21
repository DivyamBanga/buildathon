"""Record what the executor tests' class setup runs and reads.

    BR_TRACE_DIR=<dir> PYTHONPATH=/sqlglot:/tools/trace python trace_setup.py <out.json>

TestExecutor.setUpClass parses every TPC-H and TPC-DS table from CSV, which
costs more than most of the tests that follow. The selector builds those
tables once and hands them to each child, but only for a patch that touches
nothing this setup executes: that is what this trace establishes.
"""
import json
import os
import sys

sys.path.insert(0, "/repo")
import sitecustomize as tracer  # noqa: E402

from selector import runner  # noqa: E402

os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up("/sqlglot", ["tests/test_executor.py"])
import tests.test_executor as module  # noqa: E402

tracer._mark()
opened_before = set(tracer._files)
module.TestExecutor.setUpClass()
rows = [(filename[len("/sqlglot/"):], first, line) for filename, first, line in tracer._seen]
opened = sorted(entry[2 + len("/sqlglot/"):] for entry in set(tracer._files) - opened_before if entry.startswith("O "))
module.TestExecutor.tearDownClass()
units = {}
for path, first, line in rows:
    units.setdefault(path, []).append([first, line])
json.dump({"tests/test_executor.py::TestExecutor": {"units": units, "opens": opened}},
          open(sys.argv[1], "w"), separators=(",", ":"), sort_keys=True)
print({path: len(entries) for path, entries in units.items()}, len(opened), "files opened")
