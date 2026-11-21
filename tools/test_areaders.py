"""Unit checks for the read map of class attributes in selector/covmap.py.

    python tools/test_areaders.py

A tiny map directory is built in a temporary folder: eight tests that all
import sqlglot/a.py, and an areaders.bin that knows a few pairs.
"""
import json
import marshal
import os
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from selector.covmap import CovMap  # noqa: E402

PATH = "sqlglot/a.py"
ALL = 0xFF


def write(directory, name, data):
    with open(os.path.join(directory, name), "wb") as handle:
        handle.write(data)


def build(directory, with_areaders=True):
    write(directory, "tests.json", json.dumps([f"tests/t.py::T::t{i}" for i in range(8)]).encode())
    write(directory, "cov.json", json.dumps({PATH: [0, 0]}).encode())
    payload = zlib.compress(marshal.dumps(([1], [1], [0], [bytes([ALL])])))
    write(directory, "cov.bin", payload)
    write(directory, "cov.json", json.dumps({PATH: [0, len(payload)]}).encode())
    write(directory, "opens.json", b"{}")
    write(directory, "tcov.json", json.dumps({PATH: [0, 0]}).encode())
    write(directory, "tcov.bin", b"")
    write(directory, "treaders.bin", zlib.compress(marshal.dumps({})))
    if with_areaders:
        table = {
            "sqlglot.a:C|FLAG": bytes([0b00000110]),  # read by tests 1 and 2
            "sqlglot.a:C|UNREAD": b"",  # traced, read by no test
            "sqlglot.a:C.Inner|NAMES": bytes([0b10000000]),
        }
        write(directory, "areaders.bin", zlib.compress(marshal.dumps(table)))


def result(*rows):
    return {"differences": [list(row) for row in rows], "inconclusive": ""}


def row(kind, module, qualname, attribute, reason):
    return (kind, module, qualname, attribute, reason, PATH, "", 0)


checks = 0


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        raise SystemExit(f"{label}: got {got!r}, want {want!r}")


with tempfile.TemporaryDirectory() as directory:
    build(directory)
    covmap = CovMap(directory)
    flag = row("class", "sqlglot.a", "C", "FLAG", "value")
    expect("traced pair", covmap.state_candidates(result(flag)), 0b110)
    expect("traced and unread", covmap.state_candidates(result(row("class", "sqlglot.a", "C", "UNREAD", "value"))), 0)
    expect("nested class", covmap.state_candidates(result(row("class", "sqlglot.a", "C.Inner", "NAMES", "value"))), 0x80)
    expect("two pairs", covmap.state_candidates(result(flag, row("class", "sqlglot.a", "C.Inner", "NAMES", "value"))), 0x86)
    expect("untraced pair keeps the old rule", covmap.state_candidates(result(row("class", "sqlglot.a", "C", "OTHER", "value"))), ALL)
    expect("added attribute keeps the old rule", covmap.state_candidates(result(row("class", "sqlglot.a", "C", "FLAG", "added"))), ALL)
    expect("removed attribute keeps the old rule", covmap.state_candidates(result(row("class", "sqlglot.a", "C", "FLAG", "removed"))), ALL)
    expect("module attribute keeps the old rule", covmap.state_candidates(result(row("module", "sqlglot.a", "", "FLAG", "value"))), ALL)
    expect(
        "one traced, one not",
        covmap.state_candidates(result(flag, row("class", "sqlglot.a", "C", "OTHER", "value"))),
        ALL,
    )
    expect("other module", covmap.state_candidates(result(row("class", "sqlglot.b", "C", "FLAG", "value"))), ALL)
    expect("floor", covmap.attribute_readers("sqlglot.a", "C", "FLAG"), 0b110)
    expect("missing pair", covmap.attribute_readers("sqlglot.a", "C", "NOPE"), None)

with tempfile.TemporaryDirectory() as directory:
    build(directory, with_areaders=False)
    expect("no map", CovMap(directory).state_candidates(result(row("class", "sqlglot.a", "C", "FLAG", "value"))), ALL)

print(f"{checks} checks passed")
