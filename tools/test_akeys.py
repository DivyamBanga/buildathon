"""Unit checks for the key-level read map: CovMap.key_readers, the rule in state_candidates, build_akeys.

    python tools/test_akeys.py
"""
import json
import marshal
import os
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from selector.covmap import CovMap  # noqa: E402

PATH = "sqlglot/a.py"
ALL = 0xFF
checks = 0


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        raise SystemExit(f"{label}: got {got!r}, want {want!r}")


def write(directory, name, data):
    with open(os.path.join(directory, name), "wb") as handle:
        handle.write(data)


def build(directory):
    write(directory, "tests.json", json.dumps([f"tests/t.py::T::t{i}" for i in range(8)]).encode())
    payload = zlib.compress(marshal.dumps(([1], [1], [0], [bytes([ALL])])))
    write(directory, "cov.bin", payload)
    write(directory, "cov.json", json.dumps({PATH: [0, len(payload)]}).encode())
    write(directory, "opens.json", b"{}")
    write(directory, "tcov.json", json.dumps({PATH: [0, 0]}).encode())
    write(directory, "tcov.bin", b"")
    write(directory, "treaders.bin", zlib.compress(marshal.dumps({})))
    areaders = {
        "sqlglot.a:C|TABLE": bytes([0b00111110]),  # read by tests 1..5
        "sqlglot.a:C|OTHER": bytes([0b00111110]),
        "sqlglot.a:|MODULE_TABLE": b"",
    }
    write(directory, "areaders.bin", zlib.compress(marshal.dumps(areaders)))
    akeys = {
        "pairs": ["sqlglot.a:C|TABLE", "sqlglot.a:|MODULE_TABLE"],
        "all": bytes([0b00000010]),  # test 1 cannot be vouched for
        "whole": {0: bytes([0b00010000])},  # test 4 reads the table as a whole
        "keys": {
            0: {
                "str:'x'": (2,),  # test 2 looks the key up
                "num:7": bytes([0b00100000]),  # test 5, the dense encoding
            },
            1: {"str:'m'": (3,)},
        },
    }
    write(directory, "akeys.bin", zlib.compress(marshal.dumps(akeys)))


def result(*rows):
    return {"differences": [list(row) for row in rows], "inconclusive": ""}


def row(kind, module, qualname, attribute, reason, entries="absent"):
    base = [kind, module, qualname, attribute, reason, PATH, "", 0]
    if entries != "absent":
        base.append(entries)
    return base


with tempfile.TemporaryDirectory() as directory:
    build(directory)
    covmap = CovMap(directory)
    key = lambda names: covmap.key_readers("sqlglot.a", "C", "TABLE", names)  # noqa: E731
    expect("no names: no narrowing", key(None), None)
    expect("order only: whole readers and the unvouched", key([]), 0b10010)
    expect("one name", key(["str:'x'"]), 0b10110)
    expect("dense encoding", key(["num:7"]), 0b110010)
    expect("two names", key(["str:'x'", "num:7"]), 0b110110)
    expect("a name nobody looks up", key(["str:'zz'"]), 0b10010)
    expect("pair the map lacks", covmap.key_readers("sqlglot.a", "C", "OTHER", ["str:'x'"]), None)
    expect("module-level pair", covmap.key_readers("sqlglot.a", "", "MODULE_TABLE", ["str:'m'"]), 0b1010)

    # state_candidates: the readers of the attribute, narrowed by the entries.
    table = lambda entries: covmap.state_candidates(result(row("class", "sqlglot.a", "C", "TABLE", "value", entries)))  # noqa: E731
    expect("entries unknown: the attribute's readers", table(None), 0b00111110)
    expect("entries unknown, old row without the field", covmap.state_candidates(result(row("class", "sqlglot.a", "C", "TABLE", "value"))), 0b00111110)
    expect("an entry", table(["str:'x'"]), 0b10110)
    expect("an entry nobody reads", table(["str:'zz'"]), 0b10010)
    expect("order only", table([]), 0b10010)
    expect("a name of an entry and the dense one", table(["num:7", "str:'zz'"]), 0b110010)
    expect("untraced pair keeps the readers of the attribute", covmap.state_candidates(result(row("class", "sqlglot.a", "C", "OTHER", "value", ["str:'x'"]))), 0b00111110)
    expect("added entry is not a value difference", covmap.state_candidates(result(row("class", "sqlglot.a", "C", "TABLE", "added", ["str:'x'"]))), ALL)
    expect("two rows are united", covmap.state_candidates(result(
        row("class", "sqlglot.a", "C", "TABLE", "value", ["str:'x'"]), row("class", "sqlglot.a", "C", "TABLE", "value", ["num:7"]))), 0b110110)
    expect("module-level table", covmap.state_candidates(result(row("module", "sqlglot.a", "", "MODULE_TABLE", "value", ["str:'m'"]))), 0b1010)
    expect("module-level table, unknown entries", covmap.state_candidates(result(row("module", "sqlglot.a", "", "MODULE_TABLE", "value", None))), ALL)

with tempfile.TemporaryDirectory() as directory:
    build(directory)
    os.remove(os.path.join(directory, "akeys.bin"))
    covmap = CovMap(directory)
    expect("no key map: as before", covmap.state_candidates(result(row("class", "sqlglot.a", "C", "TABLE", "value", ["str:'x'"]))), 0b00111110)


def builder():
    import build_akeys

    with tempfile.TemporaryDirectory() as directory:
        maps = os.path.join(directory, "maps")
        os.makedirs(maps)
        json.dump([f"t.py::A::{i}" for i in range(6)], open(os.path.join(maps, "tests.json"), "w"))

        def run(name, rows, lines=False):
            path = os.path.join(directory, name)
            os.makedirs(path)
            json.dump({"pairs": ["m:C|T", "m:C|U"], "key_pairs": ["m:C|T", "m:C|U"], "whole_tables": []},
                      open(os.path.join(path, "pairs.json"), "w"))
            with open(os.path.join(path, "results.jsonl"), "w") as handle:
                for number, row_ in enumerate(rows):
                    row_.update({"n": number, "id": f"t.py::A::{number}", "bypass": []})
                    handle.write(json.dumps(row_) + "\n")
            return path

        def mk(keys=None, whole=(), pairs=(), touched=None, ok=True, lines="L"):
            touched = set(touched if touched is not None else list(keys or {}) + list(whole))
            return {"ok": ok, "keys": {str(p): n for p, n in (keys or {}).items()}, "whole": list(whole), "pairs": list(pairs),
                    "touched": sorted(int(t) for t in touched), "lines": lines}

        rows = [
            mk(),                                              # 0 reads nothing
            mk(keys={0: ["str:'a'"]}, pairs=[0]),               # 1 looks up a
            mk(keys={0: ["str:'a'", "str:'b'"]}, pairs=[0]),    # 2 looks up a and b
            mk(whole=[0], pairs=[0]),                          # 3 whole
            mk(pairs=[1]),                                     # 4 read pair 1, no note: whole reader of it
            mk(keys={0: ["str:'a'"]}, pairs=[0], ok=False),    # 5 failed: reads everything
        ]
        full = run("full", rows)
        base_rows = [dict(row_) for row_ in rows]
        base_rows[2] = dict(rows[2], lines="OTHER")           # the guard catches test 2
        base = run("base", base_rows)
        build_akeys.main([maps, full, "--guard", full, base])
        table = marshal.loads(zlib.decompress(open(os.path.join(maps, "akeys.bin"), "rb").read()))
        expect("pairs", table["pairs"], ["m:C|T", "m:C|U"])
        expect("everyone: the failed test and the guarded one", int.from_bytes(table["all"], "little"), 0b100100)
        expect("whole readers (not those in all)", {k: int.from_bytes(v, "little") for k, v in table["whole"].items()}, {0: 0b1000, 1: 0b10000})
        expect("names", table["keys"], {0: {"str:'a'": (1,)}})  # b is read by the guarded test only


builder()
print(f"{checks} checks passed")
