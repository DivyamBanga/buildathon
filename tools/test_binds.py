"""Unit checks for rule N: a change to which names a function binds makes the whole function the change.

    python tools/test_binds.py

selector/pydiff.py lines_to_run gives the old lines a test must execute to see a
change to one function (None: any test that runs the function).
"""
import ast
import difflib
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from selector import pydiff  # noqa: E402

checks = 0


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        print(f"FAIL {label}: got {got}, want {want}")
        sys.exit(1)


def lines_for(old_source, new_source, qualname):
    """What analyze.py works out for the changed function `qualname`."""
    old_code = compile(old_source, "m.py", "exec")
    new_code = compile(new_source, "m.py", "exec")
    diff = pydiff.diff_code(old_code, new_code)
    old_lines = old_source.splitlines(keepends=True)
    new_lines = new_source.splitlines(keepends=True)
    mapping = {}
    for tag, i1, i2, j1, _ in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                mapping[i1 + offset + 1] = j1 + offset + 1
    found = [(o, n) for o, n in diff.changed if n is not None and o.co_qualname == qualname]
    expect(f"{qualname} changed", len(found), 1)
    old, new = found[0]
    spans = {}
    for which, source, code in (("old", old_source, old), ("new", new_source, new)):
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.lineno == code.co_firstlineno:
                spans[which] = (node.lineno, node.end_lineno)
    return pydiff.lines_to_run(old, new, old_lines, new_lines, spans["old"], spans["new"], mapping.get)


# The attack patches in miniature: `exp` becomes a local of scan.
SCAN = """\
import exp


class P:
    def scan(self, source):
        if not isinstance(source, exp.Expr):
            source = 1
        if source is None:
            context, table_iter = self.static()
        else:
            context, table_iter = source, source
        return context, table_iter
"""
BOUND = SCAN.replace("context, table_iter = self.static()", "context, exp = self.static()")
expect("name stored once makes the other lines mean something else", lines_for(SCAN, BOUND, "P.scan"), None)
expect(
    "the statements alone point at the edited line",
    pydiff.changed_lines_in_function(SCAN.splitlines(keepends=True), BOUND.splitlines(keepends=True), (5, 12), (5, 12)),
    {9},
)

# Ordinary one-token edits keep their narrow set.
expect("operator swap", lines_for(SCAN, SCAN.replace("source is None", "source is not None"), "P.scan"), {8})
expect(
    "a different global becomes a builtin",
    lines_for(SCAN, SCAN.replace("isinstance(source, exp.Expr)", "isinstance(source, max.Expr)"), "P.scan"),
    {6},
)
expect(
    "a local replaced by another local that is already bound",
    lines_for(SCAN, SCAN.replace("context, table_iter = source, source", "context, table_iter = source, context"), "P.scan"),
    {11},
)

BODY = """\
VALUE = 1


def f(x):
    total = 0
    for item in x:
        total += item
    return total + VALUE
"""
expect(
    "a global becomes a local: the other loads change",
    lines_for(BODY, BODY.replace("    total = 0", "    VALUE = 0").replace("total += item", "VALUE += item"), "f"),
    None,
)
expect(
    "a new local nobody else reads",
    lines_for(BODY, BODY.replace("return total + VALUE", "extra = VALUE\n    return total + extra"), "f"),
    None,
)
expect("del of a name that was a global", lines_for(BODY, BODY.replace("    return total", "    del VALUE\n    return total"), "f"), None)
expect(
    "walrus makes a global a local",
    lines_for(BODY, BODY.replace("for item in x:", "for item in [VALUE := 2, 3]:"), "f"),
    None,
)
expect(
    "walrus over an existing local",
    lines_for(BODY, BODY.replace("total += item", "total += (item := 2)"), "f"),
    {7},
)
COMP = """\
def g(rows):
    names = [row.name for row in rows]
    return names
"""
expect(
    "a renamed comprehension variable changes nothing a test can see (no cost)",
    pydiff.diff_code(compile(COMP, "m", "exec"), compile(COMP.replace("row.name for row in", "r.name for r in"), "m", "exec")).changed,
    [],
)
SEEN = """def g(rows):
    names = [row.name for row in rows]
    return locals()
"""
expect(
    "a renamed local in a function that reads locals()",
    lines_for(SEEN, SEEN.replace("row.name for row in", "r.name for r in"), "g"),
    None,
)
expect("comprehension edited in place", lines_for(COMP, COMP.replace("row.name", "row.alias"), "g"), {2})

# global and nonlocal have no line of their own.
GLOBAL = """\
COUNT = 0


def bump():
    global COUNT
    COUNT += 1
    return COUNT
"""
expect("global removed", lines_for(GLOBAL, GLOBAL.replace("    global COUNT\n", ""), "bump"), None)
expect("global renamed", lines_for(GLOBAL, GLOBAL.replace("global COUNT", "global OTHER"), "bump"), None)
NONLOCAL = """\
def outer():
    total = 0

    def add(n):
        nonlocal total
        total += n
        return total

    return add
"""
NO_NONLOCAL = NONLOCAL.replace("        nonlocal total\n", "")
expect("nonlocal removed", lines_for(NONLOCAL, NO_NONLOCAL, "outer.<locals>.add"), None)
expect(
    "nonlocal removed: the bindings differ",
    pydiff.bindings_differ(
        compile(NONLOCAL, "m", "exec").co_consts[0].co_consts[2],
        compile(NO_NONLOCAL, "m", "exec").co_consts[0].co_consts[2],
    ),
    True,
)

# A parameter renamed to a global the body uses: signature (rule H) and bindings.
PARAM = BODY.replace("def f(x):", "def f(VALUE):").replace("in x:", "in VALUE:")
old_f_code = compile(BODY, "m", "exec").co_consts[1]
param_code = compile(PARAM, "m", "exec").co_consts[1]
expect("parameter renamed to a global: bindings", pydiff.bindings_differ(old_f_code, param_code), True)
expect("parameter renamed to a global: signature", pydiff.signature_differs(old_f_code, param_code), True)

# Free variables: the old function object cannot take the new code (rule H),
# and rule N agrees (the sets differ too).
CLOSE = """\
def make(offset):
    scale = 2

    def inner(value):
        return value + offset
    return inner
"""
new_close = CLOSE.replace("value + offset", "value + offset + scale")
old_inner = compile(CLOSE, "m", "exec").co_consts[0].co_consts[2]
new_inner = compile(new_close, "m", "exec").co_consts[0].co_consts[2]
expect("free variable gained: signature", pydiff.signature_differs(old_inner, new_inner), True)
expect("free variable gained: bindings", pydiff.bindings_differ(old_inner, new_inner), True)
expect("free variable gained: the outer binds another cell", lines_for(CLOSE, new_close, "make"), None)

# Instruction level: text unchanged, instructions not.
OLD_MODULE = "def f():\n    return x\n"
NEW_MODULE = "def outer():\n    x = 1\n\n    def f():\n        return x\n    return f\n"
def code_named(source, name):
    pending = [compile(source, "m", "exec")]
    while pending:
        code = pending.pop()
        if code.co_name == name:
            return code
        pending.extend(c for c in code.co_consts if hasattr(c, "co_code"))


old_f = code_named(OLD_MODULE, "f")
new_f = code_named(NEW_MODULE, "f")
expect(
    "same text, a global became a free variable",
    pydiff.changed_instruction_lines(old_f, new_f, lambda line: line + 3),
    {2},
)
expect("same text, same instructions", pydiff.changed_instruction_lines(old_f, old_f, lambda line: line), set())

# The compiler's own choices are not differences.
MAYBE = """\
def h(c):
    if c:
        a = 1
    return a
"""
SURE = """\
def h(c):
    if c:
        a = 1
    else:
        a = 2
    return a
"""
expect(
    "LOAD_FAST_CHECK against LOAD_FAST",
    pydiff.changed_instruction_lines(
        compile(MAYBE, "m", "exec").co_consts[0], compile(SURE, "m", "exec").co_consts[0], {1: 1, 2: 2, 3: 3, 4: 6}.get
    ),
    set(),
)
expect("constant edit", lines_for(BODY, BODY.replace("total = 0", "total = 1"), "f"), {5})


print(f"{checks} checks passed")
