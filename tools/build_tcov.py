"""Pack "what each test runs while the test itself runs" into the map.

    python build_tcov.py /data/tests.json /out/trace_test /out/trace_classes /out/maps

The first trace directory holds the split traces (tools/trace_all.py with
BR_TRACE_SPLIT=1): `L` rows for what ran before the test started, with the
whole sqlglot package imported up front, and `T` rows for what ran from then
on. The second holds the plain traces the line map (cov.bin) is built from,
where modules are imported only when the test needs them.

A line counts as run by the test itself when the split trace has it as a `T`
row, or when the plain trace has it and the split trace does not have it as an
`L` row (code that only runs when a module is imported lazily). What is left
out is what sqlglot does at import whichever test runs; a change there shows
in the state of the imported package (selector/state.py), not in a test's own
steps.

  tcov.bin, tcov.json   same layout as cov.bin, cov.json
"""
import json
import marshal
import os
import sys
import zlib
from collections import defaultdict

tests_path, split_dir, plain_dir, out_dir = sys.argv[1:5]
ROOT = "/sqlglot/"
tests = json.load(open(tests_path))["tests"]
width = (len(tests) + 7) // 8
per_file = defaultdict(lambda: defaultdict(int))
missing = 0
# A test that did not pass with sqlglot imported up front stopped part way:
# its split trace is not used.
unusable = set()
results = os.path.join(split_dir, "results.jsonl")
if os.path.exists(results):
    for row in open(results, encoding="utf-8"):
        record = json.loads(row)
        if record.get("rc") != 0:
            unusable.add(record["number"])
        else:
            unusable.discard(record["number"])
for test in tests:
    number = test["number"]
    bit = 1 << number
    split_path = os.path.join(split_dir, f"{number}.txt")
    plain = set()
    with open(os.path.join(plain_dir, f"{number}.txt"), encoding="utf-8") as handle:
        for row in handle:
            if row.startswith("L "):
                plain.add(row[2:].rstrip("\n"))
    if os.path.exists(split_path) and number not in unusable:
        at_import, in_test = set(), set()
        with open(split_path, encoding="utf-8") as handle:
            for row in handle:
                if row.startswith("L "):
                    at_import.add(row[2:].rstrip("\n"))
                elif row.startswith("T "):
                    in_test.add(row[2:].rstrip("\n"))
        rows = in_test | (plain - at_import)
    else:
        # No split trace for this test: everything it ran counts.
        missing += 1
        rows = plain
    for row in rows:
        path, first, line = row.rsplit(" ", 2)
        per_file[path[len(ROOT):]][(int(first), int(line))] |= bit

index = {}
blob = bytearray()
units = 0
for path in sorted(per_file):
    sets = {}
    firsts, lines, which = [], [], []
    for (first, line), bitmap in sorted(per_file[path].items()):
        key = bitmap.to_bytes(width, "little")
        if key not in sets:
            sets[key] = len(sets)
        firsts.append(first)
        lines.append(line)
        which.append(sets[key])
    payload = zlib.compress(marshal.dumps((firsts, lines, which, list(sets))), 9)
    index[path] = [len(blob), len(payload)]
    blob += payload
    units += len(firsts)

open(os.path.join(out_dir, "tcov.bin"), "wb").write(blob)
json.dump(index, open(os.path.join(out_dir, "tcov.json"), "w"), separators=(",", ":"))
print(f"files {len(index)} units {units} tcov.bin {len(blob)} bytes; tests without a usable split trace: "
      f"{missing} ({len(unusable)} did not pass)")
