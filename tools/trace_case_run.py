"""Run one test with the selector's own runner, in a fresh process under the tracer.

Usage: python trace_case_run.py <test_id> <result.json> [<kind> <fixture> <index|none|all>]

With a fixture given, the test sees no case of that fixture (`none`), only case
<index>, or every case (`all`); the tracer writes what ran after the first case
was handed to the test as `P` rows.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/repo")
from selector import runner  # noqa: E402

test_id, result_path = sys.argv[1], sys.argv[2]
started = time.time()
if len(sys.argv) > 3:
    kind, fixture, index = sys.argv[3:6]
    if index == "none":
        for every_kind in ("pairs", "lines", "members"):
            runner.CASE_FILTER[(every_kind, fixture)] = frozenset()
    elif index != "all":
        runner.CASE_FILTER[(kind, fixture)] = frozenset([int(index)])
    tracer = sys.modules.get("sitecustomize")

    def hook(_kind, name, _index):
        if name == fixture and tracer is not None and hasattr(tracer, "_mark"):
            tracer._mark()

    runner.CASE_HOOK = hook

os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up("/sqlglot", [test_id.split("::")[0]])
messages = []
runner.run_tests([(0, test_id)], messages.append)
failed = any(message.startswith("R 0 1") for message in messages)
times = os.times()
with open(result_path, "w") as handle:
    json.dump({
        "failed": failed,
        "elapsed": round(time.time() - started, 3),
        "cpu": round(times.user + times.system + times.children_user + times.children_system, 3),
        "calls": runner.LOADER_CALLS,
    }, handle)
