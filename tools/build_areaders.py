"""Pack the class-attribute read traces into maps/areaders.bin.

    python build_areaders.py <maps dir> <run dir> [<run dir> ...]

Each run dir is the output of tools/trace_attrs.py (the full suite, one per
hash seed; the readers of a pair are the union over the runs).

  areaders.bin    zlib-compressed marshal dict, like readers.bin:
                  "module:Qualname|attribute" -> bitmap of the tests (bit order
                  of tests.json, little-endian bytes) that read the attribute
                  once sqlglot is imported. A pair no test read is there with
                  an empty value. A pair that is not there was not traced.

A test that did not pass under tracing, died, ran out of time, or has no
result in some run, is a reader of every pair. So is a test that reads in a
process the tracer cannot follow (ESCAPING).
"""

import json
import marshal
import os
import sys
import zlib

# Tests that start another Python process: what it reads is not seen.
ESCAPING = ("tests/dialects/test_dialect.py::TestDialect::test_lazy_load",)


def load(run_dir):
    rows = {}
    with open(os.path.join(run_dir, "results.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            rows[row["n"]] = row
    with open(os.path.join(run_dir, "pairs.json"), encoding="utf-8") as handle:
        pairs = json.load(handle)["pairs"]
    return rows, pairs


def main(argv):
    maps_dir = argv[0]
    runs = argv[1:]
    ids = json.load(open(os.path.join(maps_dir, "tests.json"), encoding="utf-8"))
    width = (len(ids) + 7) // 8
    bits = {}
    everyone = 0  # tests that read every pair
    for number, test_id in enumerate(ids):
        if test_id in ESCAPING:
            everyone |= 1 << number
    for run_dir in runs:
        rows, pairs = load(run_dir)
        for pair in pairs:
            bits.setdefault(pair, 0)
        for number in range(len(ids)):
            row = rows.get(number)
            if row is None or not row["ok"]:
                everyone |= 1 << number
                continue
            for pair in row["pairs"]:
                bits[pair] |= 1 << number
    table = {pair: (bitmap | everyone).to_bytes(width, "little") if bitmap | everyone else b"" for pair, bitmap in bits.items()}
    blob = zlib.compress(marshal.dumps(table), 9)
    with open(os.path.join(maps_dir, "areaders.bin"), "wb") as handle:
        handle.write(blob)
    print(f"{len(table)} pairs, {sum(1 for value in table.values() if not value)} read by no test, "
          f"{bin(everyone).count('1')} tests read every pair, areaders.bin {len(blob)} bytes")



if __name__ == "__main__":
    main(sys.argv[1:])
