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
# ... but adding one where there was none is left to the general rule
new = BASE.replace("    def other(self):\n", '    def other(self):\n        """New."""\n')
expect("docstring added", names(analyse(new)), ["other"])

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

print(f"{checks} checks passed")
