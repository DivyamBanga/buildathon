"""Compare a pytest junit xml (run at the pinned commit) with tests.json."""
import json, sys
import xml.etree.ElementTree as ET

tests = json.load(open(sys.argv[1]))
xml = ET.parse(sys.argv[2]).getroot()
mine = {}
status = {}
for tc in xml.iter("testcase"):
    cls = tc.get("classname")  # tests.dialects.test_athena.TestAthena
    parts = cls.split(".")
    file = "/".join(parts[:-1]) + ".py"
    tid = f"{file}::{parts[-1]}::{tc.get('name')}"
    mine[tid] = float(tc.get("time"))
    kids = [c.tag for c in tc]
    status[tid] = kids
theirs = {t["id"]: t["seconds"] for t in tests["tests"]}
print("mine", len(mine), "theirs", len(theirs), "skipped-listed", tests.get("skipped"))
print("only mine:", sorted(set(mine) - set(theirs))[:20])
print("only theirs:", sorted(set(theirs) - set(mine))[:20])
print("non-pass:", {k: v for k, v in status.items() if v and v != ['system-out'] and v != ['system-err']})
order_ok = [t["id"] for t in tests["tests"]] == [k for k in mine if k in theirs]
print("same order:", order_ok)
tm = sum(mine[k] for k in theirs if k in mine); tt = sum(theirs.values())
print(f"sum mine {tm:.1f}  theirs {tt:.1f}  ratio {tm/tt:.2f}")
rows = sorted(theirs, key=lambda k: -theirs[k])[:25]
for k in rows:
    print(f"{theirs[k]:8.3f} {mine.get(k, -1):8.3f} {mine.get(k,0)/max(theirs[k],1e-9):5.2f}  {k}")
small = [k for k in theirs if theirs[k] < 0.1]
print("small tests: mine %.2f theirs %.2f" % (sum(mine[k] for k in small), sum(theirs[k] for k in small)))
