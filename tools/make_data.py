"""Assemble a scoreable data set from labeled patches.

    python tools/make_data.py <patch_dir> <labels_dir> <tests.json> <out_dir> [first] [count]

Only patches whose labeling run completed are included.
"""
import json
import os
import shutil
import sys

patches, labels, tests_path, out = sys.argv[1:5]
first = int(sys.argv[5]) if len(sys.argv) > 5 else 0
count = int(sys.argv[6]) if len(sys.argv) > 6 else 10**9
shutil.rmtree(out, ignore_errors=True)
os.makedirs(os.path.join(out, "commits"))
shutil.copy(tests_path, os.path.join(out, "tests.json"))
answers = {}
skipped = []
names = sorted(name[:-5] for name in os.listdir(labels) if name.endswith(".json"))
for name in names[first:first + count]:
    record = json.load(open(os.path.join(labels, name + ".json")))
    if record.get("status") != "ok":
        skipped.append((name, record.get("status")))
        continue
    shutil.copy(os.path.join(patches, name + ".patch"), os.path.join(out, "commits", name + ".patch"))
    answers[name] = {"failing": record["failing"], "kind": "?"}
json.dump({"commits": answers}, open(os.path.join(out, "answers.json"), "w"))
print(f"{len(answers)} commits in {out}; skipped {skipped}")
