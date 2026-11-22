"""Pack the key-level read traces of the whole tests into maps/akeys.bin.

    python build_akeys.py <maps dir> <run dir> [<run dir> ...] [--guard RUN BASE ...] [--report FILE]

Each run dir is the output of tools/trace_keys.py (the full suite, one per hash
seed); the readers of a key are the union over the runs. akeys.bin is a
zlib-compressed marshal dict:

  "pairs"  pairs whose dict-valued attribute was handed out as a recording dict
           ("module:Qualname|attribute"; "module:|NAME" for a module-level table),
           sorted; a pair that is not in the list is not traced at key level
           (use maps/areaders.bin for it)
  "all"    bitmap (bytes, little-endian, bit order of tests.json) of the tests that count as
           whole readers of every pair: tests that did not pass under the tracer, died, ran out of
           time, have no result in some run, start another Python process (ESCAPING), or
           that the guard (below) caught behaving differently with the stand-ins
  "whole"  {pair number: bitmap of the other tests that read the table as a whole}
  "keys"   {pair number: {name: readers}}, name as selector/state.py key_names gives it;
           readers: a sorted tuple of test numbers, or a bitmap (bytes) when there are many.
           A name whose readers all read the table as a whole anyway has no entry.

A test is a whole reader of a pair it read (its descriptor ran) without one note from its
table: the table reached code the recording dict does not see. A pair that was used in a way the
tracer cannot follow (a bypass event) is left out of "pairs".

--guard RUN BASE compares every test of RUN with the run of the same seed made with --no-wrap
(both with --lines): a test whose executed lines or verdict differ reads everything.
"""

import json
import marshal
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_areaders import ESCAPING  # noqa: E402

DENSE = 48  # readers of a name: a tuple up to this many tests, a bitmap above


def load(run_dir):
    rows = {}
    with open(os.path.join(run_dir, "results.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if line.endswith("\n"):
                row = json.loads(line)
                rows[row["n"]] = row
    with open(os.path.join(run_dir, "pairs.json"), encoding="utf-8") as handle:
        info = json.load(handle)
    return rows, info


def guard_differences(run_rows, base_rows):
    """{test number: why} for the tests whose verdict or executed lines differ."""
    found = {}
    for number, row in run_rows.items():
        base = base_rows.get(number)
        if base is None:
            found[number] = "no baseline"
        elif row.get("ok") != base.get("ok"):
            found[number] = "verdict"
        elif "lines" not in row or "lines" not in base:
            found[number] = "no lines"
        elif row["lines"] != base["lines"]:
            found[number] = "lines"
    return found


def pack(readers: int, width: int):
    numbers = [bit for bit in range(readers.bit_length()) if readers >> bit & 1]
    return tuple(numbers) if len(numbers) <= DENSE else readers.to_bytes(width, "little")


def main(argv):
    guards = {}
    report = None
    args = []
    position = 0
    while position < len(argv):
        if argv[position] == "--guard":
            run, base = argv[position + 1], argv[position + 2]
            guards[os.path.abspath(run)] = os.path.abspath(base)
            position += 3
        elif argv[position] == "--report":
            report = argv[position + 1]
            position += 2
        else:
            args.append(argv[position])
            position += 1
    maps_dir, run_dirs = args[0], args[1:]
    ids = json.load(open(os.path.join(maps_dir, "tests.json"), encoding="utf-8"))
    width = (len(ids) + 7) // 8
    everyone = 0
    escaping = 0
    for number, test_id in enumerate(ids):
        if test_id in ESCAPING:
            escaping |= 1 << number
    everyone |= escaping
    runs = []
    for run_dir in run_dirs:
        rows, info = load(run_dir)
        runs.append((rows, info))
    names = runs[0][1]["pairs"]
    key_pairs = runs[0][1]["key_pairs"]
    for _, info in runs:
        if info["pairs"] != names or info["key_pairs"] != key_pairs:
            raise SystemExit("the runs wrapped different pairs")
    number_of = {name: number for number, name in enumerate(names)}
    traced = {number_of[name] for name in key_pairs}
    for rows, _ in runs:
        for row in rows.values():
            for number, _how, _where in row.get("bypass", ()):
                traced.discard(number)
    order = sorted(traced, key=lambda number: names[number])
    renumber = {number: index for index, number in enumerate(order)}
    whole = {index: 0 for index in range(len(order))}
    keys = {index: {} for index in range(len(order))}
    per_run_keys = []
    guarded = {}
    failed = 0
    unnoted = 0
    for run_dir, (rows, _) in zip(run_dirs, runs):
        loose = {}
        if os.path.abspath(run_dir) in guards:
            base_rows, _ = load(guards[os.path.abspath(run_dir)])
            loose = guard_differences(rows, base_rows)
            guarded[run_dir] = loose
        seen_keys = {}
        for number in range(len(ids)):
            row = rows.get(number)
            if row is None or not row["ok"]:
                everyone |= 1 << number
                failed += 1
                continue
            if number in loose:
                everyone |= 1 << number
                continue
            touched = set(row.get("touched", ()))
            for pair, found in row["keys"].items():
                pair = int(pair)
                if pair in renumber:
                    for name in found:
                        index = renumber[pair]
                        keys[index][name] = keys[index].get(name, 0) | 1 << number
                        seen_keys.setdefault((index, name), 0)
                        seen_keys[(index, name)] |= 1 << number
            for pair in row["whole"]:
                if pair in renumber:
                    whole[renumber[pair]] |= 1 << number
            for pair in row["pairs"]:
                if pair in renumber and pair not in touched:
                    whole[renumber[pair]] |= 1 << number  # read, but through code the recording dict did not see
                    unnoted += 1
        per_run_keys.append(seen_keys)
    table = {
        "pairs": [names[number] for number in order],
        "all": everyone.to_bytes(width, "little"),
        "whole": {index: (mask & ~everyone).to_bytes(width, "little") for index, mask in whole.items() if mask & ~everyone},
        "keys": {},
    }
    for index, found in keys.items():
        entries = {}
        for name, mask in found.items():
            mask &= ~everyone & ~whole[index]
            if mask:
                entries[name] = pack(mask, width)
        if entries:
            table["keys"][index] = entries
    blob = zlib.compress(marshal.dumps(table), 9)
    with open(os.path.join(maps_dir, "akeys.bin"), "wb") as handle:
        handle.write(blob)
    count_names = sum(len(found) for found in table["keys"].values())
    lines = [
        f"{len(order)} pairs at key level ({len(key_pairs) - len(order)} dropped for a bypass), {len(names) - len(key_pairs)} traced pairs left whole",
        f"{count_names} (pair, name) entries, {len({name for found in table['keys'].values() for name in found})} distinct names, "
        f"{len(table['whole'])} pairs with whole readers",
        f"{bin(everyone).count('1')} tests read everything ({bin(escaping).count('1')} escaping, {failed} not-ok test runs), "
        f"{unnoted} (test, pair) reads with no note, akeys.bin {len(blob)} bytes",
    ]
    for run_dir, loose in guarded.items():
        reasons = {}
        for why in loose.values():
            reasons[why] = reasons.get(why, 0) + 1
        lines.append(f"guard {run_dir}: {len(loose)} tests differ {reasons}")
    if len(per_run_keys) > 1:
        first = per_run_keys[0]
        for other in per_run_keys[1:]:
            only_first = sum(1 for key in first if key not in other)
            only_other = sum(1 for key in other if key not in first)
            different = sum(1 for key in first if key in other and first[key] != other[key])
            lines.append(f"seeds: (pair, name) only in the first {only_first}, only in the other {only_other}, "
                         f"in both with other readers {different}, of {len(first)}")
    sizes = sorted(bin(int.from_bytes(raw, "little")).count("1") if isinstance(raw, bytes) else len(raw)
                   for found in table["keys"].values() for raw in found.values())
    if sizes:
        lines.append("readers per name: min %d, median %d, p90 %d, max %d" % (sizes[0], sizes[len(sizes) // 2], sizes[len(sizes) * 9 // 10], sizes[-1]))
    lines.append("pair: whole readers, names, median readers per name")
    summary = []
    for index, name in enumerate(table["pairs"]):
        whole_count = bin(int.from_bytes(table["whole"].get(index, b""), "little") | everyone).count("1")
        singles = sorted(bin(int.from_bytes(raw, "little")).count("1") if isinstance(raw, bytes) else len(raw)
                         for raw in table["keys"].get(index, {}).values())
        summary.append((whole_count, len(singles), singles[len(singles) // 2] if singles else 0, name))
    summary.sort(reverse=True)
    for whole_count, count, median, name in summary[:25]:
        lines.append(f"  {name}: {whole_count}, {count}, {median}")
    text = "\n".join(lines)
    print(text)
    if report:
        with open(report, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.write(json.dumps({"guard": {run: loose for run, loose in guarded.items()}}) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
