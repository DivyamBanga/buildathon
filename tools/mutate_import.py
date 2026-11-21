"""Generate one-token patches to code that runs at import (development data only).

    python tools/mutate_import.py <out_dir> <count> <seed>

Each patch changes one token outside every function body of a sqlglot module:
in a module or class body, a default argument, a decorator or a class keyword.
Files are chosen uniformly. The patches are labeled with real pytest runs
(tools/gt_run.py) and used to check that the selector leaves no failing test
out; nothing derived from them is in the image.
"""
import ast
import difflib
import os
import random
import sys

ROOT = os.environ.get("GEN_ROOT", "/sqlglot")
out_dir, count, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
rng = random.Random(seed)
os.makedirs(out_dir, exist_ok=True)

SOURCES = []
for base, _dirs, files in os.walk(os.path.join(ROOT, "sqlglot")):
    if "__pycache__" not in base:
        SOURCES.extend(os.path.relpath(os.path.join(base, name), ROOT) for name in files if name.endswith(".py"))
SOURCES.sort()

COMPARE = {ast.Eq: ("==", "!="), ast.NotEq: ("!=", "=="), ast.Lt: ("<", "<="), ast.LtE: ("<=", "<"),
           ast.Gt: (">", ">="), ast.GtE: (">=", ">"), ast.Is: ("is", "=="), ast.IsNot: ("is not", "!="),
           ast.In: ("in", "not in"), ast.NotIn: ("not in", "in")}


def diff(path, old, new):
    lines = list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + "".join(lines)


def sites(text):
    """(start offset, end offset, replacement, label) for every token that runs at import."""
    tree = ast.parse(text)
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line.encode("utf-8")))
    raw = text.encode("utf-8")

    def position(line, column):
        return starts[line - 1] + column

    found = []

    def visit(node, at_import):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = node.args
            outside = list(arguments.defaults) + [d for d in arguments.kw_defaults if d is not None]
            outside += getattr(node, "decorator_list", [])
            for child in outside:
                visit(child, at_import)
            body = node.body if isinstance(node.body, list) else [node.body]
            for child in body:
                visit(child, False)
            return
        if isinstance(node, ast.GeneratorExp):
            # Runs when it is iterated, which at import level is usually at once.
            for child in ast.iter_child_nodes(node):
                visit(child, at_import)
            return
        if at_import and hasattr(node, "lineno"):
            begin = position(node.lineno, node.col_offset)
            end = position(node.end_lineno, node.end_col_offset)
            if isinstance(node, ast.Constant):
                if node.value is True:
                    found.append((begin, end, b"False", "True->False"))
                elif node.value is False:
                    found.append((begin, end, b"True", "False->True"))
                elif type(node.value) is int and raw[begin:end].isdigit():
                    found.append((begin, end, str(node.value + 1).encode(), "int+1"))
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                operand = position(node.operand.lineno, node.operand.col_offset)
                if raw[begin:operand].strip() == b"not":
                    found.append((begin, operand, b"", "not-removed"))
            elif isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in COMPARE:
                old, new = COMPARE[type(node.ops[0])]
                left = position(node.left.end_lineno, node.left.end_col_offset)
                right = position(node.comparators[0].lineno, node.comparators[0].col_offset)
                between = raw[left:right]
                if between.strip() == old.encode():
                    at = left + between.index(old.encode())
                    found.append((at, at + len(old), new.encode(), f"{old}->{new}"))
            elif isinstance(node, ast.BoolOp) and len(node.values) == 2:
                old, new = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
                left = position(node.values[0].end_lineno, node.values[0].end_col_offset)
                right = position(node.values[1].lineno, node.values[1].col_offset)
                between = raw[left:right]
                if between.strip() == old.encode():
                    at = left + between.index(old.encode())
                    found.append((at, at + len(old), new.encode(), f"{old}->{new}"))
        for child in ast.iter_child_nodes(node):
            visit(child, at_import)

    visit(tree, True)
    return raw, found


made = 0
manifest = []
attempts = 0
while made < count and attempts < count * 50:
    attempts += 1
    path = rng.choice(SOURCES)
    text = open(os.path.join(ROOT, path), encoding="utf-8").read()
    raw, found = sites(text)
    if not found:
        continue
    begin, end, replacement, label = rng.choice(found)
    new = (raw[:begin] + replacement + raw[end:]).decode("utf-8")
    try:
        compile(new, path, "exec")
    except SyntaxError:
        continue
    name = f"i{seed:02d}{made:03d}"
    with open(os.path.join(out_dir, name + ".patch"), "w", encoding="utf-8", newline="") as handle:
        handle.write(diff(path, text, new))
    manifest.append(f"{name} import:{label}")
    made += 1
with open(os.path.join(out_dir, f"manifest-{seed}.txt"), "w") as handle:
    handle.write("\n".join(manifest) + "\n")
print(f"{made} patches in {out_dir}")
