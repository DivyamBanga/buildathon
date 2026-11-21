"""Unit checks: a function whose signature differs cannot be checked by swapping its code.

    python tools/test_signature.py

What a decorator or registry can read when `def` runs (parameter names, number,
order and kind, generator or coroutine, free variables, name) is fixed then;
selector/pydiff.py signature_differs says when a change touches any of it.
"""
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


SIG = textwrap.dedent('''\
    def make(offset):
        def inner(value):
            return value + offset
        return inner


    def f(this, other, position=None):
        return this.find(other, position)


    class K:
        def m(self, a, /, b, *, c=1):
            return a + b + c
''')


def signature_changes(new_source, old_source=SIG):
    old = compile(old_source, "m.py", "exec")
    new = compile(new_source, "m.py", "exec")
    diff = pydiff.diff_code(old, new)
    return sorted(
        (old_code.co_qualname, pydiff.signature_differs(old_code, new_code))
        for old_code, new_code in diff.changed
        if new_code is not None and pydiff.is_function_like(old_code)
    )


expect("body only", signature_changes(SIG.replace("return this.find", "return other.find")), [("f", False)])
expect("parameter renamed", signature_changes(SIG.replace("def f(this,", "def f(text,").replace("this.find", "text.find")), [("f", True)])
expect(
    "parameters swapped",
    signature_changes(SIG.replace("def f(this, other,", "def f(other, this,")),
    [("f", True)],
)
expect("parameter added", signature_changes(SIG.replace("position=None):", "position=None, extra=0):")), [("f", True)])
expect("varargs added", signature_changes(SIG.replace("position=None):", "position=None, *rest):")), [("f", True)])
expect("keyword only added", signature_changes(SIG.replace("position=None):", "position=None, *, extra=0):")), [("f", True)])
expect(
    "positional only moved",
    signature_changes(SIG.replace("def m(self, a, /, b,", "def m(self, a, b, /,")),
    [("K.m", True)],
)
expect(
    "becomes a generator",
    signature_changes(SIG.replace("return this.find(other, position)", "yield this.find(other, position)")),
    [("f", True)],
)
expect(
    "free variable gained",
    signature_changes(SIG.replace("return value + offset", "return value + offset + scale").replace("def make(offset):", "def make(offset):\n    scale = 2")),
    [("make", False), ("make.<locals>.inner", True)],
)
expect(
    "other local names are not a signature",
    signature_changes(SIG.replace("return this.find(other, position)", "found = this.find(other, position)\n    return found")),
    [("f", False)],
)


print(f"{checks} checks passed")
