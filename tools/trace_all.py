"""Run every test of the pinned commit alone in a fresh process under the tracer.

Usage (inside the lab image): python trace_all.py /data/tests.json /out/trace [jobs] [first] [last]
Writes /out/trace/<number>.txt (merged, de-duplicated trace) and results.jsonl.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

tests = json.load(open(sys.argv[1]))["tests"]
out = sys.argv[2]
jobs = int(sys.argv[3]) if len(sys.argv) > 3 else 4
first = int(sys.argv[4]) if len(sys.argv) > 4 else 0
last = int(sys.argv[5]) if len(sys.argv) > 5 else len(tests)
os.makedirs(out, exist_ok=True)
HERE = os.path.dirname(os.path.abspath(__file__))


def run(test):
    number = test["number"]
    final = os.path.join(out, f"{number}.txt")
    if os.path.exists(final):
        return None
    work = os.path.join(out, f"w{number}")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    env = dict(os.environ)
    env["BR_TRACE_DIR"] = work
    env["PYTHONPATH"] = "/sqlglot:" + os.path.join(HERE, "trace")
    env["SKIP_INTEGRATION"] = "0"
    started = time.time()
    # With BR_TRACE_SPLIT=1 in the environment the tracer imports sqlglot in
    # full first (L rows) and writes what runs after that as T rows.
    command = [sys.executable, "-m", "pytest", test["id"], "-q", "-p", "no:cacheprovider", "-x"]
    proc = subprocess.run(command, cwd="/sqlglot", env=env, capture_output=True, text=True)
    elapsed = time.time() - started
    lines = set()
    for name in os.listdir(work):
        with open(os.path.join(work, name), encoding="utf-8") as handle:
            lines.update(handle.read().splitlines())
    with open(final + ".tmp", "w", encoding="utf-8") as handle:
        handle.write("\n".join(sorted(lines)) + "\n")
    os.replace(final + ".tmp", final)
    shutil.rmtree(work, ignore_errors=True)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return {"number": number, "id": test["id"], "rc": proc.returncode, "elapsed": round(elapsed, 3),
            "tail": tail, "units": len(lines)}


with ThreadPoolExecutor(jobs) as pool, open(os.path.join(out, "results.jsonl"), "a") as log:
    done = 0
    for result in pool.map(run, tests[first:last]):
        done += 1
        if result is not None:
            log.write(json.dumps(result) + "\n")
            log.flush()
            if result["rc"] != 0:
                print("NONZERO", result, file=sys.stderr, flush=True)
        if done % 100 == 0:
            print(f"{done} done", file=sys.stderr, flush=True)
