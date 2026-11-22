"""Generate commits that rename one definition (a lab tool, not in the image): a removal and an addition in one patch.

    python make_renames.py /sqlglot /out/gen names.json

Adds /out/gen/commits/r_<kind>_<read|unread>_<n>.patch and extends /out/gen/manifest.json. Only the identifier
at the definition changes (what a mutation operator on a name does); every use keeps the old name. Targets
come in two groups, by what names.json says about the old name: "unread" (nothing reads it, so the patch
should be invisible) and "read" (something does, so the tests that read it are the candidates and most
fail). Kinds: module constants, module functions, class constants, methods.
"""
import ast
import difflib
import json
import os
import sys

ROOT, OUT, NAMES = sys.argv[1:4]
names = json.load(open(NAMES))
named = set(names["named"])
reads = names["globals"]
FILES = [
    "sqlglot/helper.py", "sqlglot/time.py", "sqlglot/transforms.py", "sqlglot/schema.py", "sqlglot/lineage.py",
    "sqlglot/tokens.py", "sqlglot/generator.py", "sqlglot/parser.py", "sqlglot/dialects/dialect.py",
    "sqlglot/dialects/duckdb.py", "sqlglot/generators/duckdb.py", "sqlglot/optimizer/scope.py",
    "sqlglot/optimizer/simplify.py", "sqlglot/executor/env.py", "sqlglot/executor/python.py", "sqlglot/errors.py",
]
found = {"modconst": [], "modfunc": [], "classconst": [], "method": []}


def is_read(path, name):
    return name in named or name in reads.get(path, ())


def constant(node):
    return isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper()


for path in FILES:
    source = open(os.path.join(ROOT, path), encoding="utf-8", newline="").read()
    for node in ast.parse(source).body:
        if constant(node):
            found["modconst"].append((path, node.targets[0].id, node.lineno))
        elif isinstance(node, ast.FunctionDef) and not node.decorator_list:
            found["modfunc"].append((path, node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if constant(item):
                    found["classconst"].append((path, item.targets[0].id, item.lineno))
                elif isinstance(item, ast.FunctionDef) and not item.name.startswith("__") and not item.decorator_list:
                    found["method"].append((path, item.name, item.lineno))


def pick(kind, want_read, count):
    out, seen = [], set()
    for path, name, line in found[kind]:
        if is_read(path, name) != want_read or path in seen:
            continue
        seen.add(path)  # one per file
        out.append((path, name, line))
    return out[:: max(1, len(out) // count)][:count]


manifest_path = os.path.join(OUT, "manifest.json")
manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}
os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)
total = 0
for kind, want_read, count in (
    ("modconst", False, 3), ("modconst", True, 2), ("modfunc", False, 3), ("modfunc", True, 2),
    ("classconst", False, 3), ("classconst", True, 3), ("method", False, 3), ("method", True, 3),
):
    for index, (path, name, line) in enumerate(pick(kind, want_read, count)):
        text = open(os.path.join(ROOT, path), encoding="utf-8", newline="").read()
        lines = text.split("\n")
        row = lines[line - 1]
        spot = row.index("def " + name) + 4 if kind in ("modfunc", "method") else row.index(name)
        prefix = "zz_" if kind in ("modfunc", "method") else "ZZ_"
        lines[line - 1] = row[:spot] + prefix + row[spot:]
        new_text = "\n".join(lines)
        ident = f"r_{kind}_{'read' if want_read else 'unread'}_{index}"
        body = "".join(difflib.unified_diff(text.splitlines(keepends=True), new_text.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3))
        with open(os.path.join(OUT, "commits", ident + ".patch"), "w", encoding="utf-8", newline="") as handle:
            handle.write(f"diff --git a/{path} b/{path}\n" + body)
        manifest[ident] = {
            "kind": "rename", "file": path,
            "description": f"{kind} {name} renamed to {prefix}{name} at its definition ({'read' if want_read else 'unread'})",
        }
        total += 1
with open(manifest_path, "w") as handle:
    json.dump(manifest, handle, indent=1, sort_keys=True)
print(total, "rename patches")
