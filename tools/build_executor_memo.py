"""Record what sqlglot's own executor returns for the TPC queries, and what code produces it.

    BR_TRACE_DIR=<dir> PYTHONPATH=/sqlglot:/tools/trace python build_executor_memo.py \
        /out/maps/execmemo.bin /out/maps/execphase.json

test_execute_tpch and test_execute_tpcds send every TPC query through
`sqlglot.executor.execute`: optimize, plan, run the plan. Two things are
recorded at the pinned commit:

  execmemo.bin    optimized query (hash of its serialized tree) -> the result table
  execphase.json  every line that runs from planning to the result, and while
                  the key is computed

The selector reuses a recorded result only for a patch that changes none of
the lines in execphase.json, and only for a query whose optimized form is
exactly the recorded one. Then nothing that decides the result has changed.
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
runner.install_executor_memo()

ids = [f"tests/test_executor.py::TestExecutor::{name}" for name in runner.MEMO_TESTS]
messages = []
runner.run_tests(list(enumerate(ids)), messages.append)
failed = [message for message in messages if message.startswith("R") and message.endswith(" 1\n")]
if failed:
    raise SystemExit(f"tests failed while recording: {failed}")

memo = {}
for name in os.listdir(record_dir):
    with open(os.path.join(record_dir, name), "rb") as handle:
        key, data = pickle.load(handle)
    memo[key] = data
with open(memo_path, "wb") as handle:
    handle.write(zlib.compress(pickle.dumps(memo, protocol=4), 9))
print(f"{len(memo)} results, {os.path.getsize(memo_path)} bytes")

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
