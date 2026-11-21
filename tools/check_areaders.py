"""Compare the candidates with and without the class-attribute read map, against labels.

    python tools/check_areaders.py <patch dir> <labels> <out.jsonl> [first last]

Inside the lab image, with the selector and maps next to this tool's parent
directory. For every patch the plan goes through what the coordinator does up
to the point of running tests: analyze(), then (when the plan wants the state
comparison) the comparison in a forked child, then CovMap.state_candidates
twice, without and with maps/areaders.bin, combined with the plan the way
Coordinator.finish_probe does. <labels> is a directory of <commit>.json
(failing: test numbers or ids; status ok) or an answers.json; "-" for none.

One line per commit that went through the comparison, and at the end the
labeled commits that have a failing test outside the new candidates.
"""

import json
import os
import sys

sys.path.insert(0, "/sqlglot")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
from selector import runner, state  # noqa: E402
from selector.analyze import analyze  # noqa: E402
from selector.covmap import CovMap  # noqa: E402

ROOT = "/sqlglot"
patches, labels, output = (os.path.abspath(path) if path != "-" else path for path in sys.argv[1:4])
first, last = (int(sys.argv[4]), int(sys.argv[5])) if len(sys.argv) > 5 else (0, 1 << 30)
maps = os.path.join(HERE, "maps")
os.environ["SKIP_INTEGRATION"] = "0"
ids = json.load(open(os.path.join(maps, "tests.json")))
numbered = json.load(open(os.path.join(HERE, "data", "tests.json")))["tests"]
assert [test["id"] for test in numbered] == ids, "numbers and tests.json disagree"
index = {test_id: bit for bit, test_id in enumerate(ids)}


def as_bits(failing):
    return {index[item] if isinstance(item, str) else item for item in failing}


truth = {}
if labels != "-":
    if os.path.isdir(labels):
        for name in os.listdir(labels):
            if name.endswith(".json"):
                record = json.load(open(os.path.join(labels, name)))
                if record.get("status") == "ok":
                    truth[name[:-5]] = as_bits(record["failing"])
    else:
        for commit, record in json.load(open(labels))["commits"].items():
            truth[commit] = as_bits(record["failing"])

seconds = json.load(open(os.path.join(maps, "runcost.json")))["tests"]
runner.warm_up(ROOT, list(dict.fromkeys(test_id.split("::")[0] for test_id in ids)))
state.load_pinned(ROOT)
loaded = [name for name in sys.modules if name == "sqlglot" or name.startswith("sqlglot.")]
new_map = CovMap(maps)
old_map = CovMap(maps)
old_map._packs["areaders.bin"] = None
warm = {os.path.join(ROOT, path) for path in new_map.warm_files()}


def compare(plan, sequence):
    """The state comparison of the plan's patch, in a forked child, as the coordinator runs it."""
    spec = {"overlay": f"/tmp/chk-{sequence}", "changes": state.plan_changes(plan)}
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        state.probe_main(spec, write_fd, ROOT)
        os._exit(1)
    os.close(write_fd)
    data = b""
    while True:
        chunk = os.read(read_fd, 1 << 20)
        if not chunk:
            break
        data += chunk
    os.close(read_fd)
    os.waitpid(pid, 0)
    for line in data.decode().splitlines():
        if line.startswith("E "):
            return json.loads(line[2:])
    return None


def combine(covmap, plan, result):
    own = {(path, first_line) for path, entries in plan.units.items() for first_line, _ in entries}
    narrowed = covmap.state_candidates(result, own)
    if narrowed is None:
        return None
    for path, first_line, lines in plan.warm_units:
        narrowed |= covmap.test_time(path, first_line, lines)
    return plan.rest_candidates | narrowed


def cost(bitmap):
    return sum(seconds[bit] for bit in CovMap.bits(bitmap))


rows = []
bad = []
names = sorted(name[:-6] for name in os.listdir(patches) if name.endswith(".patch"))[first:last]
with open(output, "w") as log:
    for sequence, commit in enumerate(names):
        text = open(os.path.join(patches, commit + ".patch"), encoding="utf-8", newline="").read()
        try:
            plan = analyze(commit, text, new_map, ROOT, warm)
        except Exception as error:  # noqa: BLE001
            print(commit, "analyze:", repr(error), flush=True)
            continue
        if plan.select_all or not plan.import_candidates or not plan.probe_ok:
            continue
        result = compare(plan, sequence)
        if result is None:
            print(commit, "no result from the comparison", flush=True)
            continue
        old = combine(old_map, plan, result)
        new = combine(new_map, plan, result)
        pairs = [f"{row[1]}:{row[2]}|{row[3]}({row[4]})" for row in result["differences"]]
        record = {
            "commit": commit,
            "differences": len(result["differences"]),
            "inconclusive": result["inconclusive"],
            "pairs": pairs[:12],
            "old": None if old is None else old.bit_count(),
            "new": None if new is None else new.bit_count(),
            "left_s": None if old is None or new is None else round(cost(old & ~new), 2),
            "kept_new_s": None if new is None else round(cost(new), 2),
        }
        if commit in truth:
            failing = truth[commit]
            record["failing"] = len(failing)
            covered = new if new is not None else plan.candidates
            outside = failing - set(CovMap.bits(covered | plan.unverified))
            record["outside"] = sorted(ids[bit] for bit in outside)
            if outside:
                bad.append(record)
        log.write(json.dumps(record) + "\n")
        log.flush()
        rows.append(record)

changed = [row for row in rows if row["old"] != row["new"]]
print(f"{len(rows)} commits went through the comparison, {len(changed)} change candidates, "
      f"{sum(1 for row in rows if 'failing' in row)} labeled, {len(bad)} with a failing test outside the new candidates")
for row in changed:
    print(f"  {row['commit']:12s} diffs {row['differences']:3d} old {row['old']:5d} new {row['new']:5d} "
          f"fail {row.get('failing', '-')} saved {row['left_s']} s  {row['pairs'][:3]}")
for row in bad:
    print("OUTSIDE", row["commit"], row["outside"][:6], row["pairs"][:6])
