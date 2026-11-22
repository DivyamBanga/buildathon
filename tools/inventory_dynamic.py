"""List every place the pinned code reads a name from a module or class namespace
other than by a plain attribute or global load (proof (a) of the names map).

    python inventory_dynamic.py /sqlglot

Prints `path:line  kind  source` for each. Plain attribute loads, global loads
and `from m import name` are what tools/build_names.py records; the rest is
listed here so that each site can be argued about by hand (see the README,
"Names that nothing reads").
"""
import ast
import os
import sys

root = sys.argv[1]
CALLS = {
    "getattr", "hasattr", "setattr", "delattr", "vars", "dir", "globals", "locals", "eval", "exec", "compile",
    "__import__", "type", "super", "object_to_dict", "subclasses", "getmembers", "import_module", "signature",
    "getfullargspec", "attrgetter", "methodcaller", "namedtuple", "make_dataclass", "issubclass",
}
ATTRS = {"__dict__", "__subclasses__", "__mro__", "__bases__", "__members__", "_member_map_", "__annotations__", "__slots__"}
DEFS = {"__getattr__", "__getattribute__", "__init_subclass__", "__set_name__", "__class_getitem__", "__reduce__",
        "__reduce_ex__", "__getstate__", "__setstate__", "__copy__", "__deepcopy__", "__dir__"}


def literal_name(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def scan(path, relative, out):
    with open(path, "rb") as handle:
        source = handle.read()
    lines = source.decode("utf-8", "replace").splitlines()
    tree = ast.parse(source)

    def show(node, kind):
        out.append(f"{relative}:{node.lineno}  {kind}  {lines[node.lineno - 1].strip()[:110]}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in CALLS:
                if name in ("getattr", "hasattr", "setattr", "delattr") and len(node.args) > 1:
                    kind = "attr-by-literal" if literal_name(node.args[1]) else "attr-COMPUTED"
                elif name in ("type", "super", "issubclass") and not (name == "type" and len(node.args) == 3):
                    continue
                else:
                    kind = name
                show(node, kind)
        elif isinstance(node, ast.Attribute) and node.attr in ATTRS:
            show(node, "attribute " + node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in DEFS:
            show(node, "def " + node.name)
        elif isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            show(node, "star import")
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ("__all__", "__slots__") for t in node.targets):
            show(node, "assign __all__/__slots__")
        elif isinstance(node, ast.ClassDef) and any(keyword.arg == "metaclass" for keyword in node.keywords):
            show(node, "metaclass")
        elif isinstance(node, ast.JoinedStr) or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and (
                literal_name(node.right) and node.right.value.startswith("_") or literal_name(node.left) and node.left.value.endswith("_"))):
            text = lines[node.lineno - 1]
            if any(marker in text for marker in ("getattr", "hasattr", "setattr", "_sql", "[f\"", "get(f\"")):
                show(node, "name-built-from-string")


out: list = []
for top in ("sqlglot", "sqlglotc", "tests"):
    for directory, subdirs, files in os.walk(os.path.join(root, top)):
        subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(directory, name)
                try:
                    scan(path, os.path.relpath(path, root).replace(os.sep, "/"), out)
                except SyntaxError as error:
                    out.append(f"{path}: {error}")
seen = set()
for line in out:
    if line not in seen:
        seen.add(line)
        print(line)
