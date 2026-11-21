"""Checks for selector/pydiff.py on small synthetic edits. Run: python tools/test_pydiff.py"""
import ast
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from selector import patchlib, pydiff  # noqa: E402

BASE = textwrap.dedent('''\
    """Module doc."""
    import os

    LIMIT = 10


    def top(a, b=1):
        """Doc."""
        if a is None:
            return b
        value = (
            a
            and b
            or LIMIT
        )
        for item in range(3):
            value += item
            value -= b
        return value


    class Thing:
        FLAG = True
        TABLE = {
            "x": lambda args: args[0],
            "y": lambda args: args[1],
        }

        def method(self, n):
            def inner(k):
                return k + 1

            try:
                total = inner(n)
            except ValueError:
                total = 0
            return total


    class Later:
        VALUE = 3

        def other(self):
            return self.VALUE
''')


def analyse(new):
    old_code = compile(BASE, "/x/m.py", "exec", dont_inherit=True)
    new_code = compile(new, "/x/m.py", "exec", dont_inherit=True)
    return pydiff.diff_code(old_code, new_code)


def names(diff):
    return sorted(old.co_name for old, _ in diff.changed)


def spans():
    table = {}
    for node in ast.walk(ast.parse(BASE)):
        if isinstance(node, ast.FunctionDef):
            table[node.name] = (min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno)
    return table


def lines_for(new, function):
    """Statement lines for a same-length edit inside `function`."""
    first, last = spans()[function]
    old_lines = BASE.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    shift = len(new_lines) - len(old_lines)
    return pydiff.changed_lines_in_function(old_lines, new_lines, (first, last), (first, last + shift))


def line_of(text):
    return BASE.splitlines().index(text) + 1


checks = 0


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        raise SystemExit(f"FAIL {label}: got {got!r}, want {want!r}")


# 1. comments and blank lines change nothing, even above classes
new = BASE.replace("LIMIT = 10\n", "LIMIT = 10\n# a comment\n\n")
expect("comment", names(analyse(new)), [])
new = BASE.replace("        def method(self, n):\n", "        # note\n        def method(self, n):\n")
expect("comment in class", names(analyse(new)), [])

# 2. one comparison in a function
new = BASE.replace("if a is None:", "if a == None:")
diff = analyse(new)
expect("is->==", names(diff), ["top"])
expect("is->== lines", lines_for(new, "top"), {line_of("    if a is None:")})
expect("is->== isomorphic", diff.isomorphic, True)

# 3. an operator inside a statement spanning several lines: the whole statement
new = BASE.replace("        and b\n", "        or b\n")
start = line_of("    value = (")
expect("multi-line", lines_for(new, "top"), set(range(start, start + 5)))

# 4. moving a statement out of its block is structural
new = BASE.replace("        value -= b\n", "    value -= b\n")
expect("dedent", names(analyse(new)), ["top"])
expect("dedent lines", lines_for(new, "top"), None)

# 5. a class attribute: the class body, not a function
new = BASE.replace("FLAG = True", "FLAG = False")
diff = analyse(new)
expect("class attr", names(diff), ["Thing"])
expect("class attr kind", pydiff.is_function_like(diff.changed[0][0]), False)

# 6. the body of a lambda in a class-level table: only that lambda
new = BASE.replace("args[1]", "args[2]")
diff = analyse(new)
expect("lambda", names(diff), ["<lambda>"])
expect("lambda line", diff.changed[0][0].co_firstlineno, line_of('        "y": lambda args: args[1],'))

# 7. a module constant
new = BASE.replace("LIMIT = 10", "LIMIT = 11")
expect("module", names(analyse(new)), ["<module>"])

# 8. docstrings are never read: editing one changes nothing
new = BASE.replace('"""Doc."""', '"""Other doc."""')
expect("docstring", names(analyse(new)), [])
new = BASE.replace('"""Module doc."""', '"""Another module doc."""')
expect("module docstring", names(analyse(new)), [])
# ... and so does adding one where there was none
new = BASE.replace("    def other(self):\n", '    def other(self):\n        """New."""\n')
expect("docstring added", names(analyse(new)), [])

# 8b. constants are compared by value, however the compiler numbers them
AT = BASE.count("\n") + 3  # the line the class below starts on
NUMBERED = BASE + f"\n\nclass Numbered:\n    A = {AT}\n    B = {AT + 2}\n\n    def f(self, x):\n        return {AT}\n"
OLD_BASE, BASE = BASE, NUMBERED
expect("class line", [n.lineno for n in ast.walk(ast.parse(BASE)) if getattr(n, "name", "") == "Numbered"], [AT])
# Moved two lines down, the class's first line equals one of its own constants,
# so the compiler merges them and renumbers the table.
shifted = BASE.replace("LIMIT = 10\n", "LIMIT = 10\n# moved\n# down\n")
expect("class moved onto its own constant", names(analyse(shifted)), [])
new = BASE.replace(f"    B = {AT + 2}\n", f"    B = {AT + 3}\n")
expect("constant in numbered class", names(analyse(new)), ["Numbered"])
BASE = OLD_BASE

# 9. a default value is evaluated where the function is defined
new = BASE.replace("def top(a, b=1):", "def top(a, b=2):")
expect("default", names(analyse(new)), ["<module>"])

# 10. a nested function: only the nested code object
new = BASE.replace("return k + 1", "return k - 1")
diff = analyse(new)
expect("nested", names(diff), ["inner"])
expect("nested isomorphic", diff.isomorphic, True)

# 11. an except clause
new = BASE.replace("except ValueError:", "except TypeError:")
expect("except", names(analyse(new)), ["method"])
expect("except lines", lines_for(new, "method"), {line_of("        except ValueError:")})

# 12. a statement added to a function
new = BASE.replace("        return total\n", "        total += 1\n        return total\n")
expect("insert", names(analyse(new)), ["method"])
expect("insert lines", lines_for(new, "method"), None)

# 13. a loop header
new = BASE.replace("for item in range(3):", "for item in range(4):")
expect("for lines", lines_for(new, "top"), {line_of("    for item in range(3):")})

# 14. a new method changes the class body and the shape of the tree
new = BASE.replace("    def other(self):", "    def extra(self):\n        return 1\n\n    def other(self):")
diff = analyse(new)
expect("new method", "Later" in names(diff), True)
expect("new method shape", diff.isomorphic, False)

# 15. annotations under `from __future__ import annotations` are inert strings
FUTURE = "from __future__ import annotations\n" + BASE.replace('"""Module doc."""\n', "")
OLD_BASE, BASE = BASE, FUTURE
new = BASE.replace("def top(a, b=1):", "def top(a: int, b: int = 1) -> int:")
expect("annotations added", names(analyse(new)), ["<module>"])
BASE = new
new = BASE.replace("def top(a: int, b: int = 1) -> int:", "def top(a: str, b: int = 1) -> str:")
expect("annotations edited", names(analyse(new)), [])
new = BASE.replace("def top(a: int, b: int = 1) -> int:", "def top(a: int, b: int = 2) -> int:")
expect("default with annotations", names(analyse(new)), ["<module>"])
BASE = OLD_BASE
# without the future import an annotation is evaluated, so it counts
new = BASE.replace("def top(a, b=1):", "def top(a, b: int = 1):")
expect("evaluated annotation", names(analyse(new)), ["<module>"])

# 16. assigning a new attribute on self changes the method, not the class body
new = BASE.replace("        return self.VALUE\n", "        self.seen = 1\n        return self.VALUE\n")
expect("static attributes", names(analyse(new)), ["other"])

# 17. a string inside a generator expression is not a docstring
WITH_GEN = BASE + '\n\ndef gen(items):\n    return list("abc" for _ in items)\n'
OLD_BASE, BASE = BASE, WITH_GEN
new = BASE.replace('"abc" for _', '"abd" for _')
expect("genexpr string", names(analyse(new)), ["<genexpr>"])
BASE = OLD_BASE

# 18. renaming a local variable changes nothing; renaming a parameter does
new = BASE.replace("total = inner(n)", "result = inner(n)").replace("total = 0", "result = 0").replace(
    "return total", "return result"
)
expect("local rename", names(analyse(new)), [])
new = BASE.replace("def inner(k):", "def inner(j):").replace("return k + 1", "return j + 1")
expect("parameter rename", names(analyse(new)), ["inner"])

# 19. a test file that only gains new tests or helpers
TESTS = textwrap.dedent('''\
    import unittest
    from helpers import check


    def helper(x):
        return x


    class TestA(unittest.TestCase):
        LIMIT = 3

        def test_one(self):
            self.assertEqual(helper(1), 1)
''')
KNOWN = {"TestA::test_one", "TestA::test_inherited"}


def added(new_text):
    return pydiff.only_new_definitions(TESTS, new_text, KNOWN)


expect("new test method", added(TESTS + "\n    def test_two(self):\n        self.assertTrue(True)\n"), True)
expect("new helper", added(TESTS + "\n\ndef other(y):\n    return y\n"), True)
expect("new class", added(TESTS + "\n\nclass TestB(unittest.TestCase):\n    def test_x(self):\n        pass\n"), True)
expect("replaces inherited test", added(TESTS + "\n    def test_inherited(self):\n        pass\n"), False)
expect("overrides setUp", added(TESTS + "\n    def setUp(self):\n        pass\n"), False)
expect("non-test method", added(TESTS + "\n    def validate(self):\n        pass\n"), False)
expect("module hook", added(TESTS + "\n\ndef setUpModule():\n    pass\n"), False)
expect("decorated", added(TESTS + "\n    @unittest.skip('x')\n    def test_two(self):\n        pass\n"), False)
expect("shadows a name", added(TESTS + "\n\ndef check(y):\n    return y\n"), False)
expect("edits a test", added(TESTS.replace("helper(1), 1", "helper(1), 2")), False)
expect("removes a test", added(TESTS.replace("    def test_one(self):\n        self.assertEqual(helper(1), 1)\n", "")), False)
expect("class with side effects", added(TESTS + "\n\nclass X:\n    helper(1)\n"), False)

# patchlib line mapping
patch_text = "\n".join([
    "diff --git a/m.py b/m.py", "--- a/m.py", "+++ b/m.py", "@@ -4,3 +4,4 @@", " LIMIT = 10", "+# c", " ", " ",
]) + "\n"
parsed = patchlib.parse(patch_text)[0]
patchlib.apply(parsed, BASE)
expect("apply", parsed.new_text, BASE.replace("LIMIT = 10\n", "LIMIT = 10\n# c\n"))
expect("map before", parsed.new_line(3), 3)
expect("map after", parsed.new_line(7), 8)
expect("insert point", parsed.pure_insert_after, [4])

# ---- pairing by name when definitions are added, removed, moved or renamed ----


def diff_of(old_text, new_text):
    old_code = compile(old_text, "/x/m.py", "exec", dont_inherit=True)
    new_code = compile(new_text, "/x/m.py", "exec", dont_inherit=True)
    return pydiff.diff_code(old_code, new_code)


def changed_of(diff):
    """(qualified name, whether a partner exists) of every changed code object."""
    return sorted((old.co_qualname, new is not None) for old, new in diff.changed)


def pairing_of(diff):
    """(qualified name, old first line, new first line) of every pair."""
    return sorted((old.co_qualname, old.co_firstlineno, new.co_firstlineno) for old, new in diff.pairs)


P_OLD = textwrap.dedent('''\
    import os


    def a(x):
        return x + 1


    def b(x):
        return x * 2


    class C:
        def m(self):
            return 1

        def n(self):
            return 2

        def o(self):
            return 3


    def z(x):
        return x - 1
''')

# inserted at the top, in the middle and at the end of a module: only the module body changes
HELPER = "def helper(q):\n    return q\n\n\n"
for label, anchor in (("top", "def a(x):"), ("middle", "def b(x):"), ("end", None)):
    new = P_OLD + "\n\n" + HELPER.rstrip() + "\n" if anchor is None else P_OLD.replace(anchor, HELPER + anchor, 1)
    diff = diff_of(P_OLD, new)
    expect(f"insert def {label}", changed_of(diff), [("<module>", True)])
    expect(f"insert def {label} shape", diff.isomorphic, False)
    moved = {name: now - old for name, old, now in pairing_of(diff)}
    expect(f"insert def {label} pairs every def", sorted(moved), sorted(["<module>", "a", "b", "C", "C.m", "C.n", "C.o", "z"]))
    expect(f"insert def {label} shifts", moved["z"], 0 if anchor is None else 4)

# a method inserted in a class: the class body, not the methods after it
new = P_OLD.replace("    def n(self):", "    def extra(self):\n        return 9\n\n    def n(self):")
diff = diff_of(P_OLD, new)
expect("insert method", changed_of(diff), [("C", True)])
expect("insert method pairs", {n: s for n, _, s in pairing_of(diff) if n.startswith("C.")} == {"C.m": 13, "C.n": 19, "C.o": 22}, True)

# an import added changes the module body only
expect("import added", changed_of(diff_of(P_OLD, P_OLD.replace("import os\n", "import os\nimport re\n"))), [("<module>", True)])

# a function removed: it and the module body change; the others stay paired
diff = diff_of(P_OLD, P_OLD.replace("def b(x):\n    return x * 2\n\n\n", ""))
expect("remove def", changed_of(diff), [("<module>", True), ("b", False)])

# a function renamed with its caller: removal plus addition
CALLS = P_OLD + "\n\ndef caller(v):\n    return a(v)\n"
new = CALLS.replace("def a(x):", "def a2(x):").replace("return a(v)", "return a2(v)")
expect("rename", changed_of(diff_of(CALLS, new)), [("<module>", True), ("a", False), ("caller", True)])

# two functions swapped: only the module body changes, and each pairs with itself
SWAP = P_OLD.replace(
    "def a(x):\n    return x + 1\n\n\ndef b(x):\n    return x * 2\n",
    "def b(x):\n    return x * 2\n\n\ndef a(x):\n    return x + 1\n",
)
diff = diff_of(P_OLD, SWAP)
expect("swap", changed_of(diff), [("<module>", True)])
expect(
    "swap pairs",
    sorted({(o.co_qualname, n.co_qualname) for o, n in diff.pairs}),
    sorted({(q, q) for q in ("<module>", "a", "b", "C", "C.m", "C.n", "C.o", "z")}),
)
expect("swap is not isomorphic", diff.isomorphic, False)
expect("swap and edit", changed_of(diff_of(P_OLD, SWAP.replace("x * 2", "x * 3"))), [("<module>", True), ("b", True)])

# a function moved into a class: a removal in the old scope
new = P_OLD.replace("def z(x):\n    return x - 1\n", "").rstrip("\n") + "\n"
new = new.replace("    def o(self):\n        return 3\n", "    def o(self):\n        return 3\n\n    def z(self, x):\n        return x - 1\n")
expect("moved to another scope", changed_of(diff_of(P_OLD, new)), [("<module>", True), ("C", True), ("z", False)])

# a def inserted before a nested one inside a function
NEST = "def outer(n):\n    def inner(k):\n        return k + 1\n\n    return inner(n)\n"
new = NEST.replace("    def inner(k):", "    def first(k):\n        return k\n\n    def inner(k):")
expect("nested insert", changed_of(diff_of(NEST, new)), [("outer", True)])
expect(
    "nested insert and edit",
    changed_of(diff_of(NEST, new.replace("return k + 1", "return k + 2"))),
    [("outer", True), ("outer.<locals>.inner", True)],
)

# several definitions of one name: overloads, getter and setter
STACK = textwrap.dedent('''\
    from typing import overload


    @overload
    def parse(x: int) -> int: ...
    @overload
    def parse(x: str) -> str: ...
    def parse(x):
        return x


    class P:
        @property
        def v(self):
            return self._v

        @v.setter
        def v(self, value):
            self._v = value

        def w(self):
            return 0
''')
expect("stack reformatted", changed_of(diff_of(STACK, STACK.replace("return x\n", "return x\n\n\n"))), [])
expect("stack edit last", changed_of(diff_of(STACK, STACK.replace("return x\n", "return x or 1\n"))), [("parse", True)])
expect("stack edit setter", changed_of(diff_of(STACK, STACK.replace("self._v = value", "self._v = value + 1"))), [("P.v", True)])
# a third overload: every old definition of that name is changed, the rest is not
new = STACK.replace("def parse(x):\n", "@overload\ndef parse(x: bytes) -> bytes: ...\ndef parse(x):\n")
expect("stack grows", changed_of(diff_of(STACK, new)), [("<module>", True)] + [("parse", False)] * 3)
# a setter removed
new = STACK.replace("    @v.setter\n    def v(self, value):\n        self._v = value\n\n", "")
expect("setter removed", changed_of(diff_of(STACK, new)), [("P", True), ("P.v", False), ("P.v", False)])

# conditional definitions: the k-th pairs with the k-th while the count holds
COND = "import sys\n\nif sys.version_info > (3,):\n    def f():\n        return 1\nelse:\n    def f():\n        return 2\n"
expect("conditional edit second", changed_of(diff_of(COND, COND.replace("return 2", "return 3"))), [("f", True)])
new = COND + "\nif sys:\n    def f():\n        return 4\n"
expect("conditional third", changed_of(diff_of(COND, new)), [("<module>", True), ("f", False), ("f", False)])
# a name defined twice, the later one winning, the earlier one edited
TWICE = "def f():\n    return 1\n\n\ndef f():\n    return 2\n"
expect("twice, first edited", changed_of(diff_of(TWICE, TWICE.replace("return 1", "return 0"))), [("f", True)])

# class bodies: a new class between two others
CLS = "class A:\n    X = 1\n\n\nclass B:\n    Y = 2\n"
new = "class A:\n    X = 1\n\n\nclass Mid:\n    Z = 3\n\n\nclass B:\n    Y = 2\n"
expect("class inserted", changed_of(diff_of(CLS, new)), [("<module>", True)])
expect("class renamed", changed_of(diff_of(CLS, CLS.replace("class B:", "class B2:"))), [("<module>", True), ("B", False)])

# decorated, async and generator functions pair by name like the rest
DEC = "import functools\n\n\n@functools.cache\ndef d(x):\n    return x\n\n\nasync def e(x):\n    return x\n\n\ndef g(x):\n    yield x\n"
new = DEC.replace("@functools.cache\n", "def pre():\n    pass\n\n\n@functools.cache\n")
expect("decorated after insert", changed_of(diff_of(DEC, new)), [("<module>", True)])
new = DEC.replace("async def e", "def pre():\n    pass\n\n\nasync def e").replace("yield x", "yield x + 1")
expect("async and generator after insert", changed_of(diff_of(DEC, new)), [("<module>", True), ("g", True)])

# lambdas in a class-level table: an entry inserted in the middle
TAB = "class G:\n    T = {\n        1: lambda s, e: s.a(e),\n        2: lambda s, e: s.b(e),\n        3: lambda s, e: s.c(e),\n    }\n"
diff = diff_of(TAB, TAB.replace("        2:", "        9: lambda s, e: s.z(e),\n        2:"))
expect("lambda inserted", changed_of(diff), [("G", True)])
expect(
    "lambda inserted pairs with itself",
    sorted(n.co_firstlineno - o.co_firstlineno for o, n in diff.pairs if o.co_name == "<lambda>"),
    [0, 1, 1],
)
expect("lambda removed", changed_of(diff_of(TAB, TAB.replace("        2: lambda s, e: s.b(e),\n", ""))), [("G", True), ("G.<lambda>", False)])
# one inserted and one edited: the untouched ones pair with themselves, the edited one has no safe partner
new = TAB.replace("        2: lambda s, e: s.b(e),\n", "        9: lambda s, e: s.z(e),\n        2: lambda s, e: s.b(e) + 1,\n")
expect("lambda inserted and edited", changed_of(diff_of(TAB, new)), [("G", True), ("G.<lambda>", False)])
# edited in place, nothing added: by position, as before
diff = diff_of(TAB, TAB.replace("s.b(e)", "s.b(e, 1)"))
expect("lambda edited", changed_of(diff), [("G.<lambda>", True)])
expect("lambda edited keeps the shape", diff.isomorphic, True)
# two identical lambdas: interchangeable, so pairing order cannot matter
TWIN = "class G:\n    T = {1: lambda s: 1, 2: lambda s: 1}\n"
expect("identical lambdas", changed_of(diff_of(TWIN, TWIN.replace("{1:", "{0: lambda s: 5, 1:"))), [("G", True)])
# a lambda holding another one
NEST_L = "class G:\n    T = {\n        1: lambda s: (lambda t: t + 1),\n        2: lambda s: 2,\n    }\n"
new = NEST_L.replace("        2:", "        0: lambda s: 0,\n        2:")
expect("nested lambda, entry inserted", changed_of(diff_of(NEST_L, new)), [("G", True)])
new = new.replace("t + 1", "t + 2")
expect(
    "nested lambda edited",
    changed_of(diff_of(NEST_L, new)),
    [("G", True), ("G.<lambda>", False), ("G.<lambda>.<locals>.<lambda>", False)],
)
# a lambda that is not in the table any more but whose twin elsewhere changed: count differs, all changed
new = NEST_L.replace("        2: lambda s: 2,\n", "")
expect("nested lambda removed", changed_of(diff_of(NEST_L, new)), [("G", True), ("G.<lambda>", False)])

# a scope whose own code is unchanged but whose children do not line up is not trusted
expect("last line", pydiff.last_line(compile("def f():\n    x = 1\n    return x\n", "m", "exec")), 3)

# a function whose position moved because of an insertion above it: the old lines it changed are found
# through the line mapping, and only where the paired function really stands
import difflib


def mapped_lines(old_text, new_text, function):
    """Old lines `analyze` would name for `function` (None: the whole function), as analyze does it."""
    body = "".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True), "a/m.py", "b/m.py", n=3))
    patch = patchlib.parse("diff --git a/m.py b/m.py\nindex 0000000..1111111 100644\n" + body)[0]
    patchlib.apply(patch, old_text)
    diff = pydiff.diff_code(compile(old_text, "/x/m.py", "exec"), compile(new_text, "/x/m.py", "exec"))
    for old, new in diff.changed:
        if old.co_name != function or new is None:
            continue
        first = old.co_firstlineno
        last = None
        for node in ast.walk(ast.parse(old_text)):
            if isinstance(node, ast.FunctionDef) and node.name == function:
                last = node.end_lineno
        new_first, new_last = patch.new_line(first), patch.new_line(last)
        if (
            new_first is not None
            and new_last is not None
            and new.co_firstlineno == new_first
            and pydiff.last_line(new) <= new_last
        ):
            return pydiff.changed_lines_in_function(
                old_text.splitlines(True), patch.new_text.splitlines(True), (first, last), (new_first, new_last)
            )
        return None
    return "unchanged"


SRC = "import os\n\n\ndef top(a):\n    b = a + 1\n    if b:\n        b += 2\n    return b\n\n\ndef last(x):\n    return x\n"
expect("moved function, edit inside", mapped_lines(SRC, SRC.replace("import os\n", "import os\n\n\ndef new_one():\n    return 1\n").replace("b += 2", "b += 3"), "top"), {7})
expect("moved function, nothing else", mapped_lines(SRC, SRC.replace("import os\n", "import os\n\n\ndef new_one():\n    return 1\n"), "top"), "unchanged")
# a statement appended after the old last line of the function is not cut off
SRC2 = "def top(a):\n    b = a + 1\n    if b:\n        b += 2\n\n\ndef last(x):\n    return x\n"
expect("statement appended", mapped_lines(SRC2, SRC2.replace("        b += 2\n", "        b += 2\n    b *= 9\n").replace("a + 1", "a + 3"), "top"), None)
# ... which the old code, without the guard, would have reported as just the edited line
expect("statement appended, unguarded", pydiff.changed_lines_in_function(SRC2.splitlines(True), SRC2.replace("        b += 2\n", "        b += 2\n    b *= 9\n").replace("a + 1", "a + 3").splitlines(True), (1, 4), (1, 4)), {2})
# a decorator added above: the function now starts elsewhere
DEC2 = "import os\n\n\ndef top(a):\n    return a + 1\n"
expect("decorator added", mapped_lines(DEC2, DEC2.replace("def top", "@staticmethod\ndef top").replace("a + 1", "a + 2"), "top"), None)

print(f"{checks} checks passed")
