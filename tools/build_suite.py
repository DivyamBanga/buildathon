"""Find the lines a test reaches only when it runs after the rest of the suite.

    python build_suite.py /repo/maps /out/suite /out/maps

A check, not part of the image. cov.bin holds what each test executes when it
runs alone in a fresh process. tools/trace_suite.py recorded what each test
executes inside one whole-suite run. Usually that is less (caches are warm,
modules are imported). Whatever a test executes there and NOT alone is written
here, in cov.bin's format:

  suite.bin   per source file: (code firstlineno, line) -> bitmap of the tests
              that execute that line in the suite run but not on their own
  suite.json  path -> [offset, length] into suite.bin

At the pinned commit this is 16 lines of Dialect._try_load (BR_SUITE_REPORT=1
prints them per file). A change to such a line reaches tests through what
earlier tests left behind, which no per-test map can place; the selector
selects every test for a change to a function that imports modules on demand.
"""
import json
import marshal
import os
import sys
import zlib
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from selector.covmap import CovMap  # noqa: E402

maps_dir, suite_dir, out_dir = sys.argv[1:4]
covmap = CovMap(maps_dir)
count = covmap.count
width = (count + 7) // 8
only = defaultdict(lambda: defaultdict(int))  # path -> (first, line) -> bitmap
missing = 0
rows_total = 0
for bit in range(count):
    name = os.path.join(suite_dir, f"{bit}.txt")
    if not os.path.exists(name):
        missing += 1
        continue
    mask = 1 << bit
    with open(name, encoding="utf-8") as handle:
        for row in handle:
            path, first, line = row.rstrip("\n").rsplit(" ", 2)
            first, line = int(first), int(line)
            rows_total += 1
            if not covmap.file(path).get(first, {}).get(line, 0) & mask:
                only[path][(first, line)] |= mask

index = {}
blob = bytearray()
units = 0
for path in sorted(only):
    table = only[path]
    sets = {}
    firsts, lines, which = [], [], []
    for (first, line), bitmap in sorted(table.items()):
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

os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, "suite.bin"), "wb") as handle:
    handle.write(blob)
json.dump(index, open(os.path.join(out_dir, "suite.json"), "w"), separators=(",", ":"))
print(f"{rows_total} (test, line) rows in the suite run; {missing} tests without a trace; "
      f"{units} lines in {len(only)} files are reached by some test only in the suite; suite.bin {len(blob)} bytes")
if os.environ.get("BR_SUITE_REPORT"):
    ranked = sorted(((sum(b.bit_count() for b in table.values()), len(table), path) for path, table in only.items()), reverse=True)
    for pairs, lines_count, path in ranked[:40]:
        print(f"  {path}: {lines_count} lines, {pairs} (test, line) pairs")
    tests_hit = 0
    for table in only.values():
        for bitmap in table.values():
            tests_hit |= bitmap
    print(f"  {tests_hit.bit_count()} of {count} tests reach something only in the suite")
