"""Code that runs while the whole sqlglot package is imported (run in the lab image).

    python build_importrun.py /out/trace_test /out/maps/importrun.json

The split traces (trace_all.py with BR_TRACE_SPLIT=1) import every module of
sqlglot before the test starts and write what ran until then as `L` rows. The
code objects of those rows are everything that runs at import: module and
class bodies, and the functions they call. A changed function that is not
among them only changes what happens when it is called, whichever way its
module comes to be imported.

Every trace starts with the same import, so a handful of them is read and
their rows are united.
"""
import json
import os
import sys

trace_dir, out_path = sys.argv[1:3]
ROOT = "/sqlglot/"
found = {}
names = sorted(os.listdir(trace_dir), key=lambda name: (len(name), name))
step = max(1, len(names) // 12)
used = 0
for name in names[::step]:
    with open(os.path.join(trace_dir, name), encoding="utf-8") as handle:
        for row in handle:
            if row.startswith("L "):
                _, path, first, _line = row.rstrip("\n").rsplit(" ", 3)
                if path.startswith(ROOT):
                    found.setdefault(path[len(ROOT):], set()).add(int(first))
    used += 1
json.dump({path: sorted(firsts) for path, firsts in sorted(found.items())}, open(out_path, "w"), separators=(",", ":"))
print(f"{used} traces, {len(found)} files, {sum(len(v) for v in found.values())} code objects run at import")
