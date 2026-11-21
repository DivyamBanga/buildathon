"""Record every name the pinned code and its tests can read, so that a name a patch adds or removes
can be shown to be unread (CovMap.unobservable).

    python build_names.py /sqlglot /out/maps

Writes names.json:

  named    every name some code loads or stores as an attribute (obj.name),
           imports from a module (from m import name) or deletes, and every
           name held as a string constant anywhere (getattr, hasattr, dict
           keys, __all__, __slots__, keyword arguments, mock.patch targets).
           A string such as "a.b" or "m:name" or "a, b" counts as its parts.
  globals  source path -> the names its code loads, stores or deletes as globals, or
           loads in a module or class body, that `named` does not already
           have. Only for the files of the package and for files that star
           import. A name the module's own code reads this way, or a builtin
           it uses, is read by that code and by the code of nothing else.
  stars    source path -> the source paths it star-imports (`from m import *`)

Everything under the root is read (the package, its tests, sqlglotc, the
integration tests, setup files): a reader outside the package counts as much
as one inside it. A name that code only defines (def, class, assignment
target, import binding) is not read.

The places where code reads a name without writing it (dir(), vars(),
inspect.getmembers over a namespace, getattr with a built name, the metaclass
and __init_subclass__ hooks, serde's class lookup) are not in this map; each is
argued for in the README ("Names that nothing reads") and covered by a rule in
CovMap.unobservable. tools/inventory_dynamic.py lists them.

Run it with the Python of the submission image: the opcodes below are 3.13's.
"""
import ast
import dis
import json
import os
import re
import sys
import types

GLOBAL_READS = {
    "LOAD_NAME", "LOAD_GLOBAL", "LOAD_FROM_DICT_OR_GLOBALS", "LOAD_FROM_DICT_OR_DEREF", "LOAD_CLASSDEREF",
    "DELETE_NAME", "DELETE_GLOBAL", "STORE_GLOBAL",
}
ATTR_READS = {"LOAD_ATTR", "LOAD_SUPER_ATTR", "LOAD_METHOD", "STORE_ATTR", "DELETE_ATTR", "IMPORT_FROM"}
SPLIT = re.compile(r"[.:,\s|/]+")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def string_names(value):
    """Identifiers held by a constant: the string itself, or its parts when it is a short list of them."""
    if isinstance(value, str):
        if value.isidentifier():
            yield value
            return
        parts = [part for part in SPLIT.split(value) if part]
        if 1 < len(parts) <= 8 and all(IDENTIFIER.match(part) for part in parts):
            yield from parts
    elif isinstance(value, (tuple, frozenset)):
        for item in value:
            yield from string_names(item)


def walk(code, named, read):
    for instruction in dis.get_instructions(code):
        name = instruction.opname
        if not isinstance(instruction.argval, str):
            continue
        if name in ATTR_READS:
            named.add(instruction.argval)
        elif name in GLOBAL_READS:
            read.add(instruction.argval)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            walk(const, named, read)
        else:
            named.update(string_names(const))


def star_targets(tree, dotted, is_package):
    """Dotted names of the modules `from m import *` names in this file."""
    found = set()
    package = dotted if is_package else dotted.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                found.add(".".join(base + ([node.module] if node.module else [])))
            elif node.module:
                found.add(node.module)
    return found


def main(root, out_dir):
    files = {}
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in ("__pycache__", ".git", "node_modules")]
        for name in names:
            if name.endswith(".py"):
                path = os.path.join(directory, name)
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                dotted = relative[:-3].replace("/", ".")
                is_package = dotted.endswith(".__init__")
                files[relative] = (dotted[: -len(".__init__")] if is_package else dotted, is_package)
    by_module = {dotted: relative for relative, (dotted, _) in files.items()}
    named: set = set()
    reads: dict = {}
    stars: dict = {}
    for relative, (dotted, is_package) in sorted(files.items()):
        with open(os.path.join(root, relative), "rb") as handle:
            source = handle.read()
        try:
            code = compile(source, relative, "exec", dont_inherit=True)
            tree = ast.parse(source)
        except (SyntaxError, ValueError) as error:
            # Python 2 files and the like: take their words, to be safe.
            print("not compiled:", relative, error, file=sys.stderr)
            named.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source.decode("utf-8", "replace")))
            continue
        read: set = set()
        walk(code, named, read)
        reads[relative] = read
        targets = sorted(by_module[target] for target in star_targets(tree, dotted, is_package) if target in by_module)
        if targets:
            stars[relative] = targets
    keep = {path for path in reads if path.startswith("sqlglot/")} | set(stars)
    globals_by_file = {path: sorted(reads[path] - named) for path in sorted(keep) if reads[path] - named}
    for path in sorted(keep):
        globals_by_file.setdefault(path, [])
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "names.json"), "w") as handle:
        json.dump({"named": sorted(named), "globals": globals_by_file, "stars": stars}, handle, separators=(",", ":"))
    print(len(named), "names,", len(globals_by_file), "files with globals,", len(stars), "star importers")


if __name__ == "__main__":
    main(*sys.argv[1:3])
