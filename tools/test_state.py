"""Unit checks for selector/state.py: what an import-time change leaves behind.

    python tools/test_state.py

Two small "packages" are built from source text and compared the way the
selector compares the pinned and the patched sqlglot.
"""
import enum
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
expect(
    "body and default of a function: every call sees the default, whatever lines it runs",
    differences(BASE.replace("def helper(value, scale=2)", "def helper(value, scale=3)").replace("return value * scale", "return value + scale")),
    [("", "helper", "function-meta")],
)
expect(
    "body and closure of a function",
    differences(BASE.replace("add_one = make(1)", "add_one = make(2)").replace("return value + offset", "return value - offset")),
    [("", "add_one", "function-meta"), ("", "make", "function-code")],
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


# The entries of a table that differ, by name (the ninth element of a difference row).
def entries(new_source, attribute):
    comparer = state.Comparer(world(BASE), world(new_source), "/root")
    found = [row[8] for row in comparer.run() if row[3] == attribute and row[4] == "value"]
    return found[0] if found else "no difference"


expect(
    "a set that loses a member names that member",
    entries(BASE.replace("MEMBERS = {Kind.ONE, Kind.TWO}", "MEMBERS = {Kind.ONE}"), "MEMBERS"),
    ["enum:pkg.mod:Kind:TWO"],
)
expect(
    "a table whose entry holds another value names its key (a changed function in it is reported as a function)",
    entries(BASE.replace("Kind.TWO: helper,", "Kind.TWO: None,"), "TABLE"),
    ["enum:pkg.mod:Kind:TWO"],
)
expect(
    "a new key is named, under every name an equal key has",
    entries(BASE.replace('TABLE = {**Base.TABLE, "extra": add_one}', 'TABLE = {**Base.TABLE, "extra": add_one, 1: None}'), "TABLE"),
    ["int:1", "num:1"],
)
expect("a scalar has no entries", entries(BASE.replace("LIMIT = 10", "LIMIT = 11"), "LIMIT"), None)
expect("neither has a tuple", entries(BASE.replace("SEEN = tuple({Base, Kind, Point})", "SEEN = (Base,)"), "SEEN"), None)


class Mixed(str, enum.Enum):
    HIVE = "hive"


class Counted(enum.IntEnum):
    SEVEN = 7


class Meta(type):
    def __eq__(cls, other):
        return other == "named" or type.__eq__(cls, other)

    def __hash__(cls):
        return hash("named")


class Named(metaclass=Meta):
    pass


for one, other, same in (
    ("hive", Mixed.HIVE, True), (7, Counted.SEVEN, True), (1, True, True), (1, 1.0, True), (7.0, Counted.SEVEN, True),
    ("7", 7, False), ("hive", "Hive", False), (1.5, 1, False), (b"a", "a", False), (None, 0, False),
    ((1, "hive"), (True, Mixed.HIVE), True), (frozenset({1, 2}), frozenset({2.0, True}), True), ((1, 2), (2, 1), False),
    (Mixed, Mixed, True), (Mixed, Counted, False), (complex(1, 0), 1, True),
):
    left, right = state.key_names(one), state.key_names(other)
    expect(f"key_names: {one!r} and {other!r} share a name: {same}", bool(left & right), same)
    expect(f"a dict agrees about {one!r} and {other!r}", (one == other and hash(one) == hash(other)), same)
expect("a class whose metaclass defines equality has no name", state.key_names(Named), None)
expect("neither has an object", state.key_names(object()), None)
expect("nor a tuple that holds one", state.key_names((1, object())), None)


def shapes(new_source, old_source=BASE):
    """(attribute, reason, shape) of the names added or removed; the shape is what CovMap.unobservable reads."""
    comparer = state.Comparer(world(old_source), world(new_source), "/root")
    return sorted((row[3], row[4], row[8]) for row in comparer.run() if row[4] in ("added", "removed"))


ERR = "\nclass Err(Exception):\n    pass\n"
expect("added function", shapes(BASE + "\ndef extra():\n    return 1\n"), [("extra", "added", "")])
expect("added plain class", shapes(BASE + "\nclass Plain:\n    pass\n"), [("Plain", "added", "")])
expect("added class with a base of the package", shapes(BASE + "\nclass Sub(Base):\n    pass\n"), [("Sub", "added", "R")])
expect("added class with a metaclass (an enum)", shapes(BASE + "\nclass Other(enum.Enum):\n    A = 1\n"), [("Other", "added", "R")])
expect("a module that is new to every namespace", shapes(BASE + "\nimport textwrap\n"), [("textwrap", "added", "R")])
expect("a module another name binds", shapes(BASE + "\nimport enum as en\n"), [("en", "added", "")])
expect("a module no namespace binds any more", shapes(BASE.replace("import functools\n", "").replace("half = functools.partial(helper, scale=0.5)", "half = helper")), [("functools", "removed", "R")])
expect(
    "an attribute added to a class with a base from outside",
    shapes(BASE + ERR.replace("pass", "EXTRA = 1"), BASE + ERR),
    [("EXTRA", "added", "F")],
)
expect("an attribute added to a plain class", shapes(BASE.replace("FLAG = False", "FLAG = False\n    EXTRA = 1")), [("EXTRA", "added", "")])

print(f"{checks} checks passed")
