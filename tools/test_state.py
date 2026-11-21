"""Unit checks for selector/state.py: what an import-time change leaves behind.

    python tools/test_state.py

Two small "packages" are built from source text and compared the way the
selector compares the pinned and the patched sqlglot.
"""
import os
import sys
import textwrap
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from selector import state  # noqa: E402

BASE = textwrap.dedent('''\
    import enum
    import functools
    from dataclasses import dataclass

    LIMIT = 10
    NAMES = {"a", "b"}


    class Kind(enum.Enum):
        ONE = 1
        TWO = 2


    def helper(value, scale=2):
        return value * scale


    def make(offset):
        def inner(value):
            return value + offset
        return inner


    add_one = make(1)
    half = functools.partial(helper, scale=0.5)


    @dataclass(frozen=True)
    class Point:
        x: int
        y: int = 0


    class Base:
        FLAG = False
        TABLE = {
            Kind.ONE: lambda value: value + 1,
            Kind.TWO: helper,
        }
        MEMBERS = {Kind.ONE, Kind.TWO}

        def method(self, value, strict=False):
            return value


    class Child(Base):
        TABLE = {**Base.TABLE, "extra": add_one}
        SEEN = tuple({Base, Kind, Point})
''')
checks = 0


def world(source):
    module = types.ModuleType("pkg.mod")
    module.__file__ = "/root/pkg/mod.py"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return {"pkg.mod": module}


def differences(new_source, old_source=BASE):
    comparer = state.Comparer(world(old_source), world(new_source), "/root")
    return sorted((row[2], row[3], row[4]) for row in comparer.run())


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        print(f"FAIL {label}: got {got}, want {want}")
        sys.exit(1)


expect("same source", differences(BASE), [])
expect("comment and blank lines", differences(BASE.replace("LIMIT = 10\n", "# note\n\nLIMIT = 10\n")), [])
expect("module constant", differences(BASE.replace("LIMIT = 10", "LIMIT = 11")), [("", "LIMIT", "value")])
expect("class constant", differences(BASE.replace("FLAG = False", "FLAG = True")), [("Base", "FLAG", "value")])
expect(
    "default of a function",
    differences(BASE.replace("def helper(value, scale=2)", "def helper(value, scale=3)")),
    # reported once, as the function it is, whatever else holds it (a table, a partial)
    [("", "helper", "function-meta")],
)
expect(
    "default of a method",
    differences(BASE.replace("strict=False", "strict=True")),
    [("Base", "method", "function-meta")],
)
expect(
    "closure of a function",
    differences(BASE.replace("add_one = make(1)", "add_one = make(2)")),
    [("", "add_one", "function-meta")],
)
expect(
    "body of a function held in a table",
    differences(BASE.replace("lambda value: value + 1", "lambda value: value + 2")),
    [("", "Base.<lambda>", "function-code")],
)
expect(
    "body of a named function",
    differences(BASE.replace("return value * scale", "return value + scale")),
    [("", "helper", "function-code")],
)
changed = {row[:2] for row in differences(BASE.replace("TWO = 2", "TWO = 3"))}
expect("enum value", ("Kind", "TWO") in changed, True)
expect("enum value reaches what holds the member", ("Base", "MEMBERS") in changed, True)
expect(
    "set member",
    differences(BASE.replace("MEMBERS = {Kind.ONE, Kind.TWO}", "MEMBERS = {Kind.ONE}")),
    [("Base", "MEMBERS", "value")],
)
frozen = differences(BASE.replace("frozen=True", "frozen=False"))
expect("dataclass option", {row[0] for row in frozen}, {"Point"})
expect("dataclass option changes hashing", ("Point", "__hash__", "value") in frozen, True)
expect("annotation only", differences(BASE.replace("x: int\n", "x: float\n")), [])
expect(
    "removed name",
    differences(BASE.replace("half = functools.partial(helper, scale=0.5)\n", "")),
    [("", "half", "removed")],
)
expect(
    "added name",
    differences(BASE + "\nEXTRA = 1\n"),
    [("", "EXTRA", "added")],
)
expect(
    "base class",
    [row for row in differences(BASE.replace("class Child(Base):", "class Child(Base, object):")) if row[1] == "*"],
    [("Child", "*", "bases")],
)

print(f"{checks} checks passed")
