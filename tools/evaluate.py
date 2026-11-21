"""Compare a selector output with ground truth, commit by commit.

    python tools/evaluate.py <data_dir> <output.jsonl> [manifest.txt]

<data_dir> holds tests.json, answers.json and commits/. Prints, for every commit
that is not already ideal, what it cost against the best possible cost (exactly
the failing tests, split optimally), then totals per kind of change.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from selector import schedule  # noqa: E402

data_dir, output = sys.argv[1], sys.argv[2]
manifest = {}
if len(sys.argv) > 3:
    for row in open(sys.argv[3]):
        parts = row.split()
        if len(parts) == 2:
            manifest[parts[0]] = parts[1]
tests = json.load(open(os.path.join(data_dir, "tests.json")))["tests"]
ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
ids = {t["number"]: t["id"] for t in tests}
answers = json.load(open(os.path.join(data_dir, "answers.json")))["commits"]

rows = {}
for line in open(output, encoding="utf-8"):
    if line.strip():
        row = json.loads(line)
        rows[row["commit"]] = row["workers"]

totals = {}
missed_commits = []
print(f"{'commit':8s} {'kind':9s} {'fail':>5s} {'sel':>5s} {'cost':>8s} {'ideal':>8s} {'excess':>8s}")
for commit in sorted(answers):
    truth = set(answers[commit]["failing"])
    kind = manifest.get(commit, answers[commit].get("kind", "?"))
    workers = [w for w in rows.get(commit, []) if w]
    selected = {n for w in workers for n in w}
    cost = (max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0
    ideal = schedule.cost_ms([ms[n] for n in truth])
    missing = truth - selected
    bucket = totals.setdefault(kind, {"n": 0, "cost": 0, "ideal": 0, "miss": 0})
    bucket["n"] += 1
    bucket["cost"] += cost
    bucket["ideal"] += ideal
    if missing:
        bucket["miss"] += 1
        missed_commits.append((commit, kind, sorted(missing)))
    if cost - ideal > 300 or missing:
        print(f"{commit:8s} {kind:9s} {len(truth):5d} {len(selected):5d} {cost / 1000:8.2f} {ideal / 1000:8.2f} "
              f"{(cost - ideal) / 1000:8.2f}{'  MISS ' + str(len(missing)) if missing else ''}")
print()
print(f"{'kind':10s} {'n':>4s} {'cost':>9s} {'ideal':>9s} {'excess':>9s} {'misses':>6s}")
grand = {"n": 0, "cost": 0, "ideal": 0, "miss": 0}
for kind, bucket in sorted(totals.items()):
    print(f"{kind:10s} {bucket['n']:4d} {bucket['cost'] / 1000:9.1f} {bucket['ideal'] / 1000:9.1f} "
          f"{(bucket['cost'] - bucket['ideal']) / 1000:9.1f} {bucket['miss']:6d}")
    for key in grand:
        grand[key] += bucket[key]
print(f"{'TOTAL':10s} {grand['n']:4d} {grand['cost'] / 1000:9.1f} {grand['ideal'] / 1000:9.1f} "
      f"{(grand['cost'] - grand['ideal']) / 1000:9.1f} {grand['miss']:6d}")
for commit, kind, missing in missed_commits:
    print(f"MISSED {commit} ({kind}): " + ", ".join(ids[n] for n in missing[:6]) + (" ..." if len(missing) > 6 else ""))
