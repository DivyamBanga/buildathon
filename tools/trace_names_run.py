"""Run tests alone under tools/trace_names and keep what each one listed and tried to import.

Usage (inside the lab image): python trace_names_run.py /data/tests.json /out first last [seconds]
Writes /out/<number>.txt (de-duplicated rows). Tests with a file already are skipped, so the
job can be run again; it stops starting new tests after `seconds` (default 1500).
"""
import json
import os
import shutil
import subprocess
import sys
import time

tests = json.load(open(sys.argv[1]))["tests"]
out = sys.argv[2]
first, last = int(sys.argv[3]), int(sys.argv[4])
budget = float(sys.argv[5]) if len(sys.argv) > 5 else 1500
HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(out, exist_ok=True)
started = time.time()
done = 0
for test in tests[first:last]:
    number = test["number"]
    final = os.path.join(out, f"{number}.txt")
    if os.path.exists(final):
        continue
    if time.time() - started > budget:
        break
    work = os.path.join(out, f"w{number}")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    env = dict(os.environ, BR_TRACE_DIR=work, SKIP_INTEGRATION="0",
               PYTHONPATH="/sqlglot:" + os.path.join(HERE, "trace_names"))
    subprocess.run([sys.executable, "-m", "pytest", test["id"], "-q", "-p", "no:cacheprovider", "-x"],
                   cwd="/sqlglot", env=env, capture_output=True, text=True)
    rows = set()
    for name in os.listdir(work):
        with open(os.path.join(work, name), encoding="utf-8") as handle:
            rows.update(handle.read().splitlines())
    with open(final + ".tmp", "w", encoding="utf-8") as handle:
        handle.write("\n".join(sorted(rows)) + "\n")
    os.replace(final + ".tmp", final)
    shutil.rmtree(work, ignore_errors=True)
    done += 1
print("done", done, "elapsed", round(time.time() - started), flush=True)
