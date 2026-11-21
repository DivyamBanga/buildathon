"""Cross-check the coverage tracer against sys.settrace on a sample of tests.

    python crosscheck_trace.py /data/tests.json <count> <seed> [test id substring ...]

Each test runs twice in fresh processes with the selector's runner: once under
tools/trace (sys.monitoring), once under tools/trace_settrace (sys.settrace).
Every line settrace saw must also be in the monitoring trace.
"""
import json
import os
import random
import shutil
import subprocess
import sys

tests = json.load(open(sys.argv[1]))["tests"]
count, seed = int(sys.argv[2]), int(sys.argv[3])
wanted = sys.argv[4:]
HERE = os.path.dirname(os.path.abspath(__file__))
rng = random.Random(seed)
ids = [t["id"] for t in tests if t["seconds"] < 1.5]
chosen = [i for i in ids if any(w in i for w in wanted)] + rng.sample(ids, count)


def trace(test_id, tracer_dir):
    work = f"/tmp/cc-{os.path.basename(tracer_dir)}"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    env = dict(os.environ)
    env["BR_TRACE_DIR"] = work
    env["PYTHONPATH"] = "/sqlglot:" + tracer_dir
    subprocess.run([sys.executable, os.path.join(HERE, "trace_case_run.py"), test_id, work + "/result.json"],
                   cwd="/sqlglot", env=env, capture_output=True, text=True)
    rows = set()
    for name in os.listdir(work):
        if name.endswith(".txt"):
            with open(os.path.join(work, name), encoding="utf-8") as handle:
                rows.update(row.rstrip("\n") for row in handle if row.startswith("L "))
    return rows


bad = 0
for test_id in chosen:
    monitored = trace(test_id, os.path.join(HERE, "trace"))
    reference = trace(test_id, os.path.join(HERE, "trace_settrace"))
    missing = reference - monitored
    extra = monitored - reference
    print(f"{test_id}: monitoring {len(monitored)}, settrace {len(reference)}, "
          f"missing {len(missing)}, extra {len(extra)}", flush=True)
    if missing:
        bad += 1
        for row in sorted(missing)[:12]:
            print("    missing", row)
print(f"{len(chosen)} tests checked, {bad} with lines the monitoring trace lacks")
