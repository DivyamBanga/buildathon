"""Add class-level knowledge to the map (run inside the lab image, after build_map.py).

    python build_extra.py /data/tests.json /out/trace_classes /out/maps

Writes into the maps directory:

  uses.bin      "path:Qualname" -> bitmap of tests that built an instance of the
                class or of one of its subclasses (C rows of the class trace)
  mentions.bin  class name -> bitmap of tests that executed a line, inside a
                function, that names the class
  classes.json  path -> {qualname: kind}; kind is "expr" for sqlglot expression
                classes, "dialect" for the classes that make up one dialect
                (dialects/x.py, parsers/x.py, generators/x.py), else "other"
  families.json dialect name -> every dialect whose classes it reaches at import
                (itself, the ones it subclasses or copies from)
"""
import ast
import enum
import importlib
import json
import marshal
import os
import pkgutil
import re
import sys
import zlib

tests_path, trace_dir, out_dir = sys.argv[1:4]
ROOT = "/sqlglot/"
tests = json.load(open(tests_path))["tests"]
width = (len(tests) + 7) // 8

uses = {}
for test in tests:
    bit = 1 << test["number"]
    with open(os.path.join(trace_dir, f"{test['number']}.txt"), encoding="utf-8") as handle:
        for row in handle:
            if row.startswith("C "):
                _, path, qualname = row.rstrip("\n").split(" ", 2)
                key = f"{path[len(ROOT):]}:{qualname}"
                uses[key] = uses.get(key, 0) | bit

# What every class is, from the real thing rather than from its source.
sys.path.insert(0, "/sqlglot")
import sqlglot  # noqa: E402
import sqlglot.expressions.core as core  # noqa: E402

for package in ("sqlglot", "sqlglot.dialects", "sqlglot.parsers", "sqlglot.generators",
                "sqlglot.expressions", "sqlglot.optimizer", "sqlglot.executor", "sqlglot.typing"):
    module = importlib.import_module(package)
    for info in pkgutil.iter_modules(module.__path__):
        try:
            importlib.import_module(f"{package}.{info.name}")
        except Exception as error:  # pragma: no cover
            print("skip", package, info.name, repr(error))

FAMILY = re.compile(r"^sqlglot/(dialects|parsers|generators)/(\w+)\.py$")
NOT_A_DIALECT = {"dialect", "base", "__init__"}


def family_of(path):
    match = FAMILY.match(path)
    if match and match.group(2) not in NOT_A_DIALECT:
        return match.group(2)
    return None


classes = {}
top_names = {}  # family -> names of its module-level classes
simple_names = set()
for module in list(sys.modules.values()):
    origin = getattr(module, "__file__", None) or ""
    if not origin.startswith(ROOT + "sqlglot/"):
        continue
    path = origin[len(ROOT):]
    pending = [value for value in vars(module).values()
               if isinstance(value, type) and value.__module__ == module.__name__]
    while pending:
        cls = pending.pop()
        if "<locals>" in cls.__qualname__:
            continue
        family = family_of(path)
        if issubclass(cls, enum.Enum):
            kind = "enum"
        elif family:
            kind = "dialect"
            if "." not in cls.__qualname__:
                top_names.setdefault(family, set()).add(cls.__qualname__)
        elif issubclass(cls, core.Expr):
            kind = "expr"
        else:
            kind = "other"
        classes.setdefault(path, {})[cls.__qualname__] = kind
        simple_names.add(cls.__qualname__.rsplit(".", 1)[-1])
        pending.extend(value for value in vars(cls).values()
                       if isinstance(value, type) and value.__module__ == module.__name__
                       and value.__qualname__.startswith(cls.__qualname__ + "."))

# Lines inside functions that name a class, and what each dialect's files name anywhere.
cov_index = json.load(open(os.path.join(out_dir, "cov.json")))
cov_blob = open(os.path.join(out_dir, "cov.bin"), "rb").read()


def cov_file(path):
    entry = cov_index.get(path)
    if entry is None:
        return {}
    firsts, lines, which, sets = marshal.loads(zlib.decompress(cov_blob[entry[0]:entry[0] + entry[1]]))
    decoded = [int.from_bytes(raw, "little") for raw in sets]
    return {(first, line): decoded[index] for first, line, index in zip(firsts, lines, which)}


mentions = {}
family_names = {}  # family -> every identifier its files use

# Class-level constants: where each is assigned, and who reads it.
class_statements = {}  # path -> qualname -> {"span": [first, last], "assigns": [[first, last, name]]}
constant_names = set()


def index_classes(path, body, prefix):
    for node in body:
        if not isinstance(node, ast.ClassDef):
            continue
        qualname = prefix + node.name
        assigns = []
        for statement in node.body:
            target = None
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                target = statement.target
            if isinstance(target, ast.Name):
                assigns.append([statement.lineno, statement.end_lineno, target.id])
                constant_names.add(target.id)
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        class_statements.setdefault(path, {})[qualname] = {"span": [first, node.end_lineno], "assigns": assigns}
        index_classes(path, node.body, qualname + ".")


def named_assign(statement):
    target = None
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        target = statement.target
    return target.id if isinstance(target, ast.Name) else None


module_statements = {}  # path -> [[first, last, name]] for module-level `NAME = value`
sources = {}
for path in sorted(cov_index):
    if path.endswith(".py"):
        sources[path] = ast.parse(open(ROOT + path, encoding="utf-8").read())
        if path.startswith("sqlglot/"):
            index_classes(path, sources[path].body, "")
            assigns = []
            for statement in sources[path].body:
                name = named_assign(statement)
                if name:
                    assigns.append([statement.lineno, statement.end_lineno, name])
                    constant_names.add(name)
            module_statements[path] = assigns

readers = {}  # constant name -> tests that execute a line, inside a function, that reads it
read_at_import = set()  # constant names read by a class or module body


class Visitor(ast.NodeVisitor):
    def __init__(self, table, family):
        self.table = table
        self.scope = None
        self.family = family

    def note(self, name, line, reads=True):
        if self.family is not None:
            family_names.setdefault(self.family, set()).add(name)
        found = self.table.get((self.scope, line)) if self.scope is not None else None
        if name in simple_names and found:
            mentions[name] = mentions.get(name, 0) | found
        if reads and name in constant_names:
            if self.scope is None:
                read_at_import.add(name)
            elif found:
                readers[name] = readers.get(name, 0) | found

    def visit_Name(self, node):
        self.note(node.id, node.lineno, isinstance(node.ctx, ast.Load))

    def visit_Attribute(self, node):
        reads = isinstance(node.ctx, ast.Load)
        self.note(node.attr, node.end_lineno, reads)
        self.note(node.attr, node.lineno, reads)
        self.generic_visit(node)

    def visit_Constant(self, node):
        # getattr(x, "NAME") and "NAME" in vars(x) read it too.
        if isinstance(node.value, str) and node.value in constant_names:
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                self.note(node.value, line)

    def scoped(self, node, first):
        saved, self.scope = self.scope, first
        self.generic_visit(node)
        self.scope = saved

    def visit_FunctionDef(self, node):
        for extra in node.decorator_list + node.args.defaults + [d for d in node.args.kw_defaults if d]:
            self.visit(extra)
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        saved, self.scope = self.scope, first
        for statement in node.body:
            self.visit(statement)
        self.scope = saved

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.scoped(node, node.lineno)

    def visit_GeneratorExp(self, node):
        self.scoped(node, node.lineno)

    def visit_ClassDef(self, node):
        saved, self.scope = self.scope, None
        self.generic_visit(node)
        self.scope = saved


for path, tree in sources.items():
    Visitor(cov_file(path), family_of(path)).visit(tree)

# A dialect reaches another when its files name one of the other's classes.
reach = {family: {family} for family in top_names}
for family, names in family_names.items():
    for other, tops in top_names.items():
        if other != family and names & tops:
            reach.setdefault(family, {family}).add(other)
changed = True
while changed:
    changed = False
    for family, others in reach.items():
        for other in list(others):
            extra = reach.get(other, set()) - others
            if extra:
                others |= extra
                changed = True
# Invert: who is affected when `family` changes.
affected = {family: sorted(other for other, reached in reach.items() if family in reached)
            for family in reach}


def pack(table):
    return zlib.compress(marshal.dumps({key: value.to_bytes(width, "little") for key, value in table.items()}), 9)


open(os.path.join(out_dir, "uses.bin"), "wb").write(pack(uses))
open(os.path.join(out_dir, "mentions.bin"), "wb").write(pack(mentions))
open(os.path.join(out_dir, "readers.bin"), "wb").write(pack(readers))
json.dump({"classes": class_statements, "modules": module_statements, "read_at_import": sorted(read_at_import)},
          open(os.path.join(out_dir, "constants.json"), "w"), separators=(",", ":"), sort_keys=True)
json.dump(classes, open(os.path.join(out_dir, "classes.json"), "w"), separators=(",", ":"), sort_keys=True)
json.dump({"affected": affected, "tops": {k: sorted(v) for k, v in top_names.items()}},
          open(os.path.join(out_dir, "families.json"), "w"), separators=(",", ":"), sort_keys=True)
print(f"uses {len(uses)} classes, mentions {len(mentions)} names, "
      f"{sum(len(v) for v in classes.values())} classes in {len(classes)} files, {len(affected)} dialect families")
