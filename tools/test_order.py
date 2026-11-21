"""Unit checks: a table whose order is the same in every fresh import is compared in order.

    python tools/test_order.py

selector/state.py STABLE_ORDER lists such attributes (maps/order.json); the order of a
table or of a tuple of classes that is not listed, and of sets, stays irrelevant.
"""
import os
import sys
import textwrap
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from selector import state  # noqa: E402

checks = 0


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        print(f"FAIL {label}: got {got}, want {want}")
        sys.exit(1)


BASE = textwrap.dedent('''\
    import enum
    import functools


    class Kind(enum.Enum):
        ONE = 1
        TWO = 2


    class A:
        pass


    class B:
        pass


    class Holder:
        ARGS = {"this": True, "start": False, "length": False}
        NESTED = {"outer": {"a": 1, "b": 2}, "list": [{"x": 1, "y": 2}]}
        BY_KIND = {Kind.ONE: "one", Kind.TWO: "two"}
        CLASSES = (A, B)
        LOOSE = {"p": 1, "q": 2}
        LOOSE_CLASSES = (A, B)


    TABLE = {"a": 1, "b": 2}


    def with_default(x, table={"m": 1, "n": 2}, *, named={"u": 1, "v": 2}):
        return x


    def make_closure():
        held = {"c": 1, "d": 2}

        def inner():
            return held

        return inner


    closure = make_closure()
    bound = functools.partial(with_default, table={"e": 1, "f": 2})


    class Box:
        def __init__(self):
            self.table = {"g": 1, "h": 2}


    box = Box()
''')


def world(source):
    module = types.ModuleType("pkg.mod")
    module.__file__ = "/root/pkg/mod.py"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return {"pkg.mod": module}


STABLE = frozenset(
    [
        ("pkg.mod", "Holder", "ARGS"),
        ("pkg.mod", "Holder", "NESTED"),
        ("pkg.mod", "Holder", "BY_KIND"),
        ("pkg.mod", "Holder", "CLASSES"),
        ("pkg.mod", "", "TABLE"),
        ("pkg.mod", "", "with_default"),
        ("pkg.mod", "", "closure"),
        ("pkg.mod", "", "bound"),
        ("pkg.mod", "", "box"),
    ]
)


def differences(new_source, stable=STABLE):
    state.STABLE_ORDER = stable
    comparer = state.Comparer(world(BASE), world(new_source), "/root")
    return sorted((row[2], row[3], row[4]) for row in comparer.run())


expect("same source", differences(BASE), [])
expect(
    "stable table reordered",
    differences(BASE.replace('"start": False, "length": False', '"length": False, "start": False')),
    [("Holder", "ARGS", "value")],
)
expect(
    "stable table reordered, nothing known",
    differences(BASE.replace('"start": False, "length": False', '"length": False, "start": False'), frozenset()),
    [],
)
expect(
    "stable module table reordered",
    differences(BASE.replace('TABLE = {"a": 1, "b": 2}', 'TABLE = {"b": 2, "a": 1}')),
    [("", "TABLE", "value")],
)
expect(
    "enum keys reordered",
    differences(BASE.replace('{Kind.ONE: "one", Kind.TWO: "two"}', '{Kind.TWO: "two", Kind.ONE: "one"}')),
    [("Holder", "BY_KIND", "value")],
)
expect(
    "inner table reordered",
    differences(BASE.replace('{"a": 1, "b": 2}, "list"', '{"b": 2, "a": 1}, "list"')),
    [("Holder", "NESTED", "value")],
)
expect(
    "table inside a list reordered",
    differences(BASE.replace('{"x": 1, "y": 2}', '{"y": 2, "x": 1}')),
    [("Holder", "NESTED", "value")],
)
expect(
    "outer table of a nest reordered",
    differences(BASE.replace('{"outer": {"a": 1, "b": 2}, "list": [{"x": 1, "y": 2}]}', '{"list": [{"x": 1, "y": 2}], "outer": {"a": 1, "b": 2}}')),
    [("Holder", "NESTED", "value")],
)
expect(
    "classes reordered in a stable tuple",
    differences(BASE.replace("CLASSES = (A, B)", "CLASSES = (B, A)")),
    [("Holder", "CLASSES", "value")],
)
expect(
    "classes reordered in a tuple that varies",
    differences(BASE.replace("LOOSE_CLASSES = (A, B)", "LOOSE_CLASSES = (B, A)")),
    [],
)
expect(
    "table that varies, reordered",
    differences(BASE.replace('{"p": 1, "q": 2}', '{"q": 2, "p": 1}')),
    [],
)
expect(
    "table that varies, a value changed",
    differences(BASE.replace('{"p": 1, "q": 2}', '{"q": 3, "p": 1}')),
    [("Holder", "LOOSE", "value")],
)
expect(
    "stable table, a value changed",
    differences(BASE.replace('"start": False', '"start": True')),
    [("Holder", "ARGS", "value")],
)
expect(
    "stable table, a key added",
    differences(BASE.replace('"start": False,', '"start": False, "zero": False,')),
    [("Holder", "ARGS", "value")],
)

expect(
    "default of a function reordered",
    differences(BASE.replace('{"m": 1, "n": 2}', '{"n": 2, "m": 1}')),
    [("", "with_default", "function-meta")],
)
expect(
    "keyword default of a function reordered",
    differences(BASE.replace('{"u": 1, "v": 2}', '{"v": 2, "u": 1}')),
    [("", "with_default", "function-meta")],
)
expect(
    "table in a closure reordered",
    differences(BASE.replace('{"c": 1, "d": 2}', '{"d": 2, "c": 1}')),
    [("", "closure", "function-meta"), ("", "make_closure", "function-code")],
)
expect(
    "keywords of a partial reordered",
    differences(BASE.replace('{"e": 1, "f": 2}', '{"f": 2, "e": 1}')),
    [("", "bound", "value")],
)
expect(
    "table held by an instance reordered",
    differences(BASE.replace('{"g": 1, "h": 2}', '{"h": 2, "g": 1}')),
    [("", "box", "value"), ("Box", "__init__", "function-code")],
)
expect(
    "default reordered, nothing known",
    differences(BASE.replace('{"m": 1, "n": 2}', '{"n": 2, "m": 1}'), frozenset()),
    [],
)
expect(
    "default of a function with a value changed",
    differences(BASE.replace('{"m": 1, "n": 2}', '{"m": 1, "n": 3}')),
    [("", "with_default", "function-meta")],
)

# what the walk records
expect("signature of a plain value", state.order_signature(5), "")
expect("signature of a one-entry table", state.order_signature({"a": 1}), "")
expect(
    "signature follows the order",
    state.order_signature({"a": 1, "b": 2}) != state.order_signature({"b": 2, "a": 1}),
    True,
)
expect(
    "signature sees the order of a list of classes",
    state.order_signature([int, str]) != state.order_signature([str, int]),
    True,
)
expect("signature ignores the order of a list of numbers", state.order_signature([1, 2]), "")


def _sig_of(source, name):
    return state.order_signature(world(source)["pkg.mod"].__dict__[name])


for name, old, new in (
    ("with_default", '{"m": 1, "n": 2}', '{"n": 2, "m": 1}'),
    ("closure", '{"c": 1, "d": 2}', '{"d": 2, "c": 1}'),
    ("bound", '{"e": 1, "f": 2}', '{"f": 2, "e": 1}'),
    ("box", '{"g": 1, "h": 2}', '{"h": 2, "g": 1}'),
):
    expect(f"signature sees a table held by {name}", _sig_of(BASE, name) != _sig_of(BASE.replace(old, new), name), True)

print(f"{checks} checks passed")
