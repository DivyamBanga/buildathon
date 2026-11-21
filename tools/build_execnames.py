"""Names that the executor phase can read (run after build_executor_memo.py).

    python build_execnames.py /out/maps/execphase.json /out/maps/execnames.json

execphase.json lists every line `sqlglot.executor.execute` runs outside the
optimizer at the pinned commit. A constant of a module or class is read by
name, so the phase can only see one whose name is written in a function it
runs. This lists those names, by the rule of the reader map (build_extra.py):
a name, an attribute, or a part of a string constant. It errs on the wide
side: every name anywhere in a top-level function or method that has at least
one executed line counts (nested functions, defaults and decorators included),
and a line outside any function (a module or class body that the phase
imported) makes every name in the file count.
"""
import ast
import json
import re
import sys

phase_path, out_path = sys.argv[1:3]
ROOT = "/sqlglot/"
CONSTANT_LIKE = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
phase = json.load(open(phase_path))


def names_in(node):
    found = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name):
            found.add(inner.id)
        elif isinstance(inner, ast.Attribute):
            found.add(inner.attr)
        elif isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            found.update(re.split(r"\W+", inner.value))
        elif isinstance(inner, ast.alias):
            found.update(inner.name.split("."))
        elif isinstance(inner, (ast.Global, ast.Nonlocal)):
            found.update(inner.names)
    return found


def outer_functions(body):
    """Functions and methods that are not inside another function, with their line spans."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            yield first, node.end_lineno, node
        elif isinstance(node, ast.ClassDef):
            yield from outer_functions(node.body)
        else:
            # Functions nested in compound statements at module or class level (if/try/with).
            for field in ("body", "orelse", "finalbody", "handlers"):
                inner = getattr(node, field, None)
                if isinstance(inner, list):
                    yield from outer_functions([item for item in inner if isinstance(item, ast.AST)])


names = set()
functions = 0
whole_files = []
for path, rows in sorted(phase.items()):
    tree = ast.parse(open(ROOT + path, encoding="utf-8").read())
    spans = list(outer_functions(tree.body))
    # Lambdas and generator expressions written outside any function (entries
    # of a module-level or class-level table) are code objects of their own.
    loose = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Lambda, ast.GeneratorExp))
        and not any(first <= node.lineno <= last for first, last, _ in spans)
    ]
    used = set()
    for code_first, line in rows:
        for index, (first, last, _node) in enumerate(spans):
            if first <= line <= last:
                used.add(index)
                break
        else:
            holders = [node for node in loose if node.lineno == code_first and node.lineno <= line <= node.end_lineno]
            if not holders:
                # A module or class body ran inside the phase: anything in the file may have been read.
                used = None
                break
            for node in holders:
                names |= names_in(node)
    if used is None:
        whole_files.append(path)
        names |= names_in(tree)
        continue
    functions += len(used)
    for index in used:
        names |= names_in(spans[index][2])

constants = sorted(name for name in names if CONSTANT_LIKE.match(name))
json.dump({"names": constants}, open(out_path, "w"), separators=(",", ":"))
print(f"{len(phase)} files, {functions} functions, {len(constants)} constant names; whole files: {whole_files}")
