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

Where a name can be read from, in lines ("where" in the output): a name or
an attribute written in the code is read by the statement it stands in, so
by the lines of that statement (for an `if`, a loop, a `with` and the like:
the lines of its header). A name that only occurs inside a string, an import
or a `global` line may be put together and used anywhere in the function:
for those the whole function counts, as before.
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


_BODIES = ("body", "orelse", "finalbody", "handlers", "cases")


def read_spans(function):
    """{name: {(first line, last line)}} for the names and attributes written in a function:
    the lines of the statement each one stands in."""
    parents = {}
    for node in ast.walk(function):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        else:
            continue
        holder = node
        while holder is not function and not isinstance(holder, (ast.stmt, ast.excepthandler, ast.match_case)):
            holder = parents[holder]
        first = min(getattr(holder, "lineno", node.lineno), node.lineno)
        last = getattr(holder, "end_lineno", node.end_lineno)
        inner = [
            item
            for field in _BODIES
            for item in (getattr(holder, field, None) or [])
            if isinstance(item, ast.AST) and hasattr(item, "lineno")
        ]
        if inner:
            # A compound statement: what is written in its header is read when the header runs.
            starts = [min([item.lineno] + [d.lineno for d in getattr(item, "decorator_list", [])]) for item in inner]
            last = max(min(starts) - 1, node.end_lineno, first)
        found.setdefault(name, set()).add((first, max(last, node.end_lineno)))
    return found


def loose_names(node):
    """Names that occur in a string, an import or a global line: they can be read anywhere in the function."""
    found = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
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
where = {}  # name -> [[path, first line, last line]] of the code that mentions it
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
                for name in names_in(node):
                    where.setdefault(name, []).append([path, node.lineno, node.end_lineno])
    if used is None:
        whole_files.append(path)
        names |= names_in(tree)
        for name in names_in(tree):
            where.setdefault(name, []).append([path, 1, 10**9])
        continue
    functions += len(used)
    for index in used:
        first, last, node = spans[index]
        names |= names_in(node)
        whole = loose_names(node)
        written = read_spans(node)
        for name in names_in(node):
            if name in whole or name not in written:
                where.setdefault(name, []).append([path, first, last])
            else:
                for start, end in sorted(written[name]):
                    where.setdefault(name, []).append([path, start, end])

constants = sorted(name for name in names if CONSTANT_LIKE.match(name))
# "where" holds every name the phase's code mentions, not only the constants: a
# name of a module in lower case (a function, an object made at import) is read
# by the statements that name it just the same.
spans_of = {name: sorted(map(list, {tuple(span) for span in where[name]})) for name in sorted(names) if name.isidentifier()}
# Lines of the phase that can see a name nobody wrote down: a lookup by a
# computed text, or a walk over a namespace. The selector applies its rule for
# names that appear or vanish only while this list is the one it has reviewed.
WALKERS = {
    "vars", "dir", "globals", "locals", "getmembers", "getmembers_static", "attrgetter", "eval", "exec",
    "__dict__", "__slots__", "__all__", "__subclasses__", "__mro__", "mro", "__getattribute__", "__getattr__",
}
dynamic = set()
for path, rows in sorted(phase.items()):
    tree = ast.parse(open(ROOT + path, encoding="utf-8").read())
    executed = {line for _first, line in rows}
    for node in ast.walk(tree):
        if not hasattr(node, "lineno") or not any(line in executed for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr", "setattr", "delattr"):
            if len(node.args) < 2 or not (isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
                dynamic.add((path, node.lineno))
        elif (isinstance(node, ast.Name) and node.id in WALKERS) or (isinstance(node, ast.Attribute) and node.attr in WALKERS):
            dynamic.add((path, node.lineno))
json.dump(
    {"names": constants, "where": spans_of, "dynamic": sorted(map(list, dynamic)), "every_name": True},
    open(out_path, "w"),
    separators=(",", ":"),
)
print(f"lines that look a name up by a computed text or walk a namespace: {sorted(dynamic)}")
print(f"{len(phase)} files, {functions} functions, {len(constants)} constant names; whole files: {whole_files}")
