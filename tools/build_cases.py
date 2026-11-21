"""Pack the per-case traces (tools/trace_cases.py) into the map.

    python build_cases.py /out/cases /out/maps

  cases.json  "bit@fixture" -> {kind, count, weight: [per-case share of the test's cost]}
              plus the (test, fixture) pairs whose cases make no difference to the test
  cases.bin   per source file, per pair:
                zero   (code firstlineno, line) the test runs with no case at all
                cases  (code firstlineno, line) -> bitmap of the cases that run it
  casesrc.json source path -> [offset, length] into cases.bin
"""
import json
import marshal
import os
import sys
import zlib
from collections import defaultdict

cases_dir, out_dir = sys.argv[1:3]
ROOT = "/sqlglot/"
summary = json.load(open(os.path.join(cases_dir, "summary.json")))


def rows(path):
    found = defaultdict(list)
    with open(path, encoding="utf-8") as handle:
        for row in handle:
            row = row.rstrip("\n")
            if row:
                source, first, line = row.rsplit(" ", 2)
                found[source[len(ROOT):]].append((int(first), int(line)))
    return found


pairs = {}
independent = []
per_source = defaultdict(dict)  # source -> key -> (zero list, {unit: bitmap})
for key, info in sorted(summary.items()):
    bit, fixture = key.split("@", 1)
    if info.get("independent"):
        independent.append([int(bit), fixture])
        continue
    if not info.get("sliced") or info.get("failed_cases"):
        continue
    name = fixture.replace("/", "__")
    count = info["count"]
    costs = []
    for source, units in rows(os.path.join(cases_dir, f"{bit}@{name}@none.txt")).items():
        per_source[source].setdefault(key, ([], {}))[0].extend(units)
    for index in range(count):
        result = json.load(open(os.path.join(cases_dir, f"{bit}@{name}@{index}.json")))
        costs.append(max(0.002, result["cpu"] - info["none_cpu"]))
        mask = 1 << index
        for source, units in rows(os.path.join(cases_dir, f"{bit}@{name}@{index}.txt")).items():
            table = per_source[source].setdefault(key, ([], {}))[1]
            for unit in units:
                table[unit] = table.get(unit, 0) | mask
    total = sum(costs)
    pairs[key] = {"kind": info["kind"], "count": count,
                  "weight": [round(cost / total, 5) for cost in costs]}

index = {}
blob = bytearray()
for source in sorted(per_source):
    packed = {}
    for key, (zero, table) in per_source[source].items():
        width = (pairs[key]["count"] + 7) // 8
        sets = {}
        firsts, lines, which = [], [], []
        for (first, line), bitmap in sorted(table.items()):
            raw = bitmap.to_bytes(width, "little")
            if raw not in sets:
                sets[raw] = len(sets)
            firsts.append(first)
            lines.append(line)
            which.append(sets[raw])
        zero = sorted(set(zero))
        packed[key] = ([unit[0] for unit in zero], [unit[1] for unit in zero], firsts, lines, which, list(sets))
    payload = zlib.compress(marshal.dumps(packed), 9)
    index[source] = [len(blob), len(payload)]
    blob += payload

open(os.path.join(out_dir, "cases.bin"), "wb").write(blob)
json.dump(index, open(os.path.join(out_dir, "casesrc.json"), "w"), separators=(",", ":"))
json.dump({"pairs": pairs, "independent": independent},
          open(os.path.join(out_dir, "cases.json"), "w"), separators=(",", ":"))
print(f"sliced pairs {len(pairs)}, independent {len(independent)}, sources {len(index)}, cases.bin {len(blob)} bytes")
