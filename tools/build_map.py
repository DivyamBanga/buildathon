"""Turn the per-test traces into the committed dependency map.

Usage (inside the lab image):
    python build_map.py /data/tests.json /out/trace /out/maps [/out/warm.txt]

Everything in the map is derived from running the pinned commit's own tests:

  tests.json   test ids, in bit order
  cov.bin      per source file: (code firstlineno, line) -> bitmap of tests that
               executed that line of that code object in an isolated process
  cov.json     path -> [offset, length] into cov.bin
  opens.json   non-Python file -> tests that opened it
  funcs.json   path -> {def firstlineno: end line} for every def in the pinned source
  warm.json    path -> [code firstlineno] of code objects that already ran while
               importing every test module (so swapping them in place is not safe)
"""
import ast
import json
import marshal
import os
import sys
import zlib
from collections import defaultdict

tests_path, trace_dir, out_dir = sys.argv[1:4]
warm_path = sys.argv[4] if len(sys.argv) > 4 else None
ROOT = "/sqlglot/"
tests = json.load(open(tests_path))["tests"]
os.makedirs(out_dir, exist_ok=True)

count = len(tests)
width = (count + 7) // 8
per_file = defaultdict(lambda: defaultdict(int))  # path -> (first, line) -> bitmap int
opens = defaultdict(list)
for test in tests:
    number = test["number"]
    bit = 1 << number
    with open(os.path.join(trace_dir, f"{number}.txt"), encoding="utf-8") as handle:
        for row in handle:
            row = row.rstrip("\n")
            if row.startswith("L "):
                _, path, first, line = row.rsplit(" ", 3)
                per_file[path[len(ROOT):]][(int(first), int(line))] |= bit
            elif row.startswith("O "):
                path = row[2 + len(ROOT):]
                if not path.endswith((".py", ".pyc")):
                    opens[path].append(number)

index = {}
blob = bytearray()
unit_total = 0
set_total = 0
for path in sorted(per_file):
    units = per_file[path]
    sets = {}
    firsts, lines, which = [], [], []
    for (first, line), bitmap in sorted(units.items()):
        key = bitmap.to_bytes(width, "little")
        if key not in sets:
            sets[key] = len(sets)
        firsts.append(first)
        lines.append(line)
        which.append(sets[key])
    payload = zlib.compress(marshal.dumps((firsts, lines, which, list(sets))), 9)
    index[path] = [len(blob), len(payload)]
    blob += payload
    unit_total += len(firsts)
    set_total += len(sets)

with open(os.path.join(out_dir, "cov.bin"), "wb") as handle:
    handle.write(blob)
json.dump(index, open(os.path.join(out_dir, "cov.json"), "w"), separators=(",", ":"))
json.dump([test["id"] for test in tests], open(os.path.join(out_dir, "tests.json"), "w"),
          separators=(",", ":"))
json.dump({path: sorted(numbers) for path, numbers in sorted(opens.items())},
          open(os.path.join(out_dir, "opens.json"), "w"), separators=(",", ":"))

funcs = {}
for path in sorted(per_file):
    source = open(ROOT + path, encoding="utf-8").read()
    table = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            table[first] = [node.lineno, node.end_lineno]
    funcs[path] = table
json.dump(funcs, open(os.path.join(out_dir, "funcs.json"), "w"), separators=(",", ":"))

if warm_path:
    warm = defaultdict(set)
    for row in open(warm_path, encoding="utf-8"):
        if row.startswith("L "):
            _, path, first, _line = row.rstrip("\n").rsplit(" ", 3)
            warm[path[len(ROOT):]].add(int(first))
    json.dump({path: sorted(firsts) for path, firsts in sorted(warm.items())},
              open(os.path.join(out_dir, "warm.json"), "w"), separators=(",", ":"))

print(f"files {len(index)} units {unit_total} distinct sets {set_total} cov.bin {len(blob)} bytes")
