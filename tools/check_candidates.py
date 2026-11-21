"""Check the candidate sets against labeled patches, without running any test.

    python tools/check_candidates.py <patch_dir> <labels_dir> [manifest]

Every test that really fails must be a candidate (or, when a patched module no
longer imports, belong to a file the runner's import check covers).
"""
import json
import os
import sys

sys.path.insert(0, "/repo")
from selector.analyze import analyze  # noqa: E402
from selector.covmap import CovMap  # noqa: E402

patches, labels = sys.argv[1], sys.argv[2]
kinds = {}
if len(sys.argv) > 3:
    for row in open(sys.argv[3]):
        parts = row.split()
        if len(parts) == 2:
            kinds[parts[0]] = parts[1]
covmap = CovMap("/repo/maps")
loaded = {os.path.join("/sqlglot", path) for path in covmap.warm_files()}
bad = 0
total = 0
show_all = os.environ.get("BR_SHOW_ALL")
for name in sorted(os.listdir(patches)):
    if not name.endswith(".patch"):
        continue
    commit = name[:-6]
    label = os.path.join(labels, commit + ".json")
    if os.path.exists(label):
        record = json.load(open(label))
        if record.get("status") != "ok":
            continue
        total += 1
    elif show_all:
        record = {"failing": []}
    else:
        continue
    text = open(os.path.join(patches, commit + ".patch"), encoding="utf-8", newline="").read()
    plan = analyze(commit, text, covmap, "/sqlglot", loaded)
    failing = set(record["failing"])
    candidates = set(covmap.bits(plan.candidates))
    checked = set(covmap.bits(plan.import_check))
    missing = set() if plan.select_all else failing - candidates
    covered = missing <= checked
    flag = ""
    if missing:
        flag = "import-check" if covered else "MISS"
        if not covered:
            bad += 1
    mode = "all" if plan.select_all else ("clean" if plan.needs_clean else "hot")
    print(f"{commit} {kinds.get(commit, '?'):9s} fail={len(failing):4d} cand={len(candidates):4d} "
          f"check={len(checked):4d} {mode:5s} {flag}" + ("  " + plan.notes[0][:110] if show_all and plan.notes else ""))
    if flag == "MISS":
        print("    missing:", [covmap.ids[n] for n in sorted(missing - checked)][:5])
        print("    notes:", "; ".join(plan.notes)[:600])
print(f"{total} labeled patches, {bad} with a failing test outside the candidates")
