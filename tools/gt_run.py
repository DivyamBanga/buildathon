"""Label patches the way the organisers did: apply to a fresh copy of the pinned
commit and run the whole suite with pytest.

Usage (inside the lab image): python gt_run.py <tests.json> <out_dir> <patch>...
Writes <out_dir>/<id>.json = {"failing": [numbers], "seconds": wall, "status": ...}
"""
import json
import os
import shutil
import subprocess
import sys
import time

tests_path, out_dir = sys.argv[1], sys.argv[2]
tests = json.load(open(tests_path))["tests"]
number = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
os.makedirs(out_dir, exist_ok=True)
TIMEOUT = os.environ.get("BR_GT_TIMEOUT", "60")
SESSION = int(os.environ.get("BR_GT_SESSION", "1500"))

for patch in sys.argv[3:]:
    name = os.path.basename(patch)[: -len(".patch")]
    target = os.path.join(out_dir, name + ".json")
    if os.path.exists(target):
        continue
    work = f"/tmp/gt-{name}"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree("/sqlglot", work, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    applied = subprocess.run(["git", "apply", "--whitespace=nowarn", os.path.abspath(patch)], cwd=work,
                             capture_output=True, text=True)
    record = {"status": "ok"}
    if applied.returncode:
        record = {"status": "apply-failed", "detail": applied.stderr[-300:], "failing": []}
    else:
        env = dict(os.environ)
        env["PYTHONPATH"] = work + ":" + os.path.dirname(os.path.abspath(__file__))
        env["SKIP_INTEGRATION"] = "0"
        env["BR_GT_OUT"] = f"/tmp/gt-{name}.json"
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        started = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-p", "gt_plugin",
                 f"--timeout={TIMEOUT}"],
                cwd=work, env=env, capture_output=True, text=True, timeout=SESSION,
            )
            record["rc"] = proc.returncode
            record["tail"] = proc.stdout.strip().splitlines()[-1:] if proc.stdout.strip() else []
        except subprocess.TimeoutExpired:
            record["status"] = "session-timeout"
        record["seconds"] = round(time.time() - started, 1)
        failing = set()
        if os.path.exists(env["BR_GT_OUT"]):
            for node in json.load(open(env["BR_GT_OUT"])):
                if node.startswith("COLLECT::"):
                    failing.update(by_file.get(node[len("COLLECT::"):], []))
                elif node in number:
                    failing.add(number[node])
                else:
                    record.setdefault("unknown", []).append(node)
        elif record["status"] == "ok":
            record["status"] = "no-report"
        record["failing"] = sorted(failing)
    with open(target + ".tmp", "w") as handle:
        json.dump(record, handle)
    os.replace(target + ".tmp", target)
    shutil.rmtree(work, ignore_errors=True)
    print(name, record["status"], len(record["failing"]), record.get("seconds"), flush=True)
