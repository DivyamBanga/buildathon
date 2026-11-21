"""Trace fixture-driven tests one fixture case at a time.

Usage (inside the lab image): python trace_cases.py /data/tests.json /repo/maps /out/cases [jobs]

For every (test, .sql fixture it opens):
  none   the test with no case of the fixture: what it runs regardless of cases
  all    the whole test, to know how many cases it reads and what a full run costs
  <k>    the test with only case k: rows marked P are what case k made it run
"""
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

tests = json.load(open(sys.argv[1]))["tests"]
maps, out = sys.argv[2], sys.argv[3]
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 4
ids = json.load(open(os.path.join(maps, "tests.json")))
opens = json.load(open(os.path.join(maps, "opens.json")))
HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = "tests/fixtures/"


def run(bit, fixture, kind, index):
    name = fixture.replace("/", "__")
    final = os.path.join(out, f"{bit}@{name}@{index}")
    if os.path.exists(final + ".json"):
        return json.load(open(final + ".json"))
    work = final + ".w"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    env = dict(os.environ)
    env["BR_TRACE_DIR"] = work
    env["PYTHONPATH"] = "/sqlglot:" + os.path.join(HERE, "trace")
    env.pop("BR_TRACE_CLASSES", None)
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "trace_case_run.py"), ids[bit], final + ".tmp", kind, fixture, str(index)],
        cwd="/sqlglot", env=env, capture_output=True, text=True,
    )
    rows = set()
    for entry in os.listdir(work):
        with open(os.path.join(work, entry), encoding="utf-8") as handle:
            for row in handle:
                if row[:2] in ("L ", "P "):
                    rows.add(row.rstrip("\n"))
    # A case run keeps what ran after the case was handed over; the two
    # reference runs keep everything.
    wanted = ("P ",) if index not in ("none", "all") else ("L ", "P ")
    with open(final + ".txt", "w", encoding="utf-8") as handle:
        handle.write("\n".join(sorted({row[2:] for row in rows if row.startswith(wanted)})) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    if not os.path.exists(final + ".tmp"):
        result = {"failed": True, "error": proc.stderr[-400:]}
    else:
        result = json.load(open(final + ".tmp"))
        os.remove(final + ".tmp")
    with open(final + ".json", "w") as handle:
        json.dump(result, handle)
    return result


pairs = []
for fixture, numbers in sorted(opens.items()):
    if fixture.startswith(PREFIX) and fixture.endswith(".sql"):
        for bit in numbers:
            pairs.append((bit, fixture[len(PREFIX):]))
# Tests that loop over a module-level collection instead of a fixture file.
sys.path.insert(0, "/repo")
from selector import runner  # noqa: E402

for module_name, _attribute, name in runner.ENUMERATIONS:
    prefix = module_name.replace(".", "/") + ".py::"
    for bit, test_id in enumerate(ids):
        if test_id.startswith(prefix):
            pairs.append((bit, name))
print(len(pairs), "test/fixture pairs", flush=True)

with ThreadPoolExecutor(jobs) as pool:
    firsts = list(pool.map(lambda pair: (pair, run(pair[0], pair[1], "pairs", "none"),
                                         run(pair[0], pair[1], "pairs", "all")), pairs))
    todo = []
    summary = {}
    for (bit, fixture), none, full in firsts:
        calls = [call for call in full.get("calls", []) if call[1] == fixture]
        key = f"{bit}@{fixture}"
        if none.get("failed") or full.get("failed") or not calls:
            summary[key] = {"sliced": False, "why": "fails without cases" if calls else "no loader call"}
            continue
        kind, _name, count = calls[0]
        if any(call[0] != kind or call[2] != count for call in calls):
            summary[key] = {"sliced": False, "why": "mixed loader calls"}
            continue
        name = fixture.replace("/", "__")
        with open(os.path.join(out, f"{bit}@{name}@none.txt")) as a, open(
            os.path.join(out, f"{bit}@{name}@all.txt")
        ) as b:
            if a.read() == b.read():
                # The cases make no difference to what this test runs.
                summary[key] = {"sliced": False, "why": "independent", "independent": True}
                continue
        summary[key] = {"sliced": True, "kind": kind, "count": count,
                        "none_cpu": none["cpu"], "full_cpu": full["cpu"]}
        todo.extend((bit, fixture, kind, index) for index in range(count))
    print(len(todo), "case runs", flush=True)
    done = 0
    for (bit, fixture, kind, index), result in zip(todo, pool.map(lambda job: run(*job), todo)):
        done += 1
        if result.get("failed"):
            summary[f"{bit}@{fixture}"].setdefault("failed_cases", []).append(index)
        if done % 500 == 0:
            print(done, "done", flush=True)
json.dump(summary, open(os.path.join(out, "summary.json"), "w"), indent=1)
print("sliced:", sum(1 for v in summary.values() if v["sliced"]), "of", len(summary))
