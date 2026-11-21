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


# The same, counting only what a test runs while the test itself runs (build_tcov.py).
tcov_index = None
if os.path.exists(os.path.join(out_dir, "tcov.json")):
    tcov_index = json.load(open(os.path.join(out_dir, "tcov.json")))
    tcov_blob = open(os.path.join(out_dir, "tcov.bin"), "rb").read()


def tcov_file(path):
    entry = tcov_index.get(path) if tcov_index is not None else None
    if entry is None:
        return {}
    firsts, lines, which, sets = marshal.loads(zlib.decompress(tcov_blob[entry[0]:entry[0] + entry[1]]))
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


def line_owners(text, path):
    """line -> first lines of the function-like code objects with instructions on it.

    Where a name is written and which code object reads it are two things: the
    first iterable of a generator expression and the defaults of a lambda are
    evaluated by the enclosing function. The compiled code says which.
    """
    owners = {}
    pending = [compile(text, path, "exec", dont_inherit=True)]
    while pending:
        code = pending.pop()
        if code.co_flags & 0x0002:  # CO_NEWLOCALS: a def, lambda or generator expression
            for _start, _end, line in code.co_lines():
                if line is not None:
                    owners.setdefault(line, set()).add(code.co_firstlineno)
        pending.extend(const for const in code.co_consts if hasattr(const, "co_code"))
    return owners


module_statements = {}  # path -> [[first, last, name]] for module-level `NAME = value`
sources = {}
owners_of = {}
for path in sorted(cov_index):
    if path.endswith(".py"):
        text = open(ROOT + path, encoding="utf-8").read()
        sources[path] = ast.parse(text)
        owners_of[path] = line_owners(text, path)
        if path.startswith("sqlglot/"):
            index_classes(path, sources[path].body, "")
        # Module-level constants of the test helpers count too (the TPC schemas).
        assigns = []
        for statement in sources[path].body:
            name = named_assign(statement)
            if name:
                assigns.append([statement.lineno, statement.end_lineno, name])
                constant_names.add(name)
        module_statements[path] = assigns

readers = {}  # constant name -> tests that execute a line, inside a function, that reads it
# constant name -> tests that read it once sqlglot is imported: a line inside a
# function that the test itself runs, or any line of a test file (a test module
# may keep what it read while it was imported).
test_readers = {}
read_at_import = set()  # constant names read by a class or module body
# Names written like constants are tracked wherever they are assigned (a
# metaclass sets `klass._QUOTES`, not a class body), so that every such name
# has a reader set, possibly empty.
CONSTANT_LIKE = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
IMPORTERS = {"import_module", "__import__", "entry_points"}
loaders = {}  # path -> first lines of the functions that import modules on demand
EVERY_TEST = (1 << len(tests)) - 1


class Visitor(ast.NodeVisitor):
    def __init__(self, table, family, test_table=None, test_file=False, path=""):
        self.path = path
        self.owners = owners_of.get(path, {})
        self.table = table
        self.test_table = test_table or {}
        self.test_file = test_file
        self.scope = None
        self.scope_depth = 0
        self.loader = False
        self.body = 1  # first line of the module or class body being visited
        self.family = family

    def ran(self, table, line):
        """Tests that ran `line` as part of a function: under the code objects that
        have instructions on it, or, when none has (the compiler put them on
        another line), under the whole function it is written in."""
        firsts = self.owners.get(line)
        if firsts:
            found = 0
            for first in firsts:
                found |= table.get((first, line), 0)
            return found
        found = 0
        for (first, _line), bitmap in table.items():
            if first == self.scope:
                found |= bitmap
        return found

    def note(self, name, line, reads=True):
        if self.family is not None:
            family_names.setdefault(self.family, set()).add(name)
        found = self.ran(self.table, line) if self.scope is not None else None
        if name in simple_names and found:
            mentions[name] = mentions.get(name, 0) | found
        if name in constant_names or CONSTANT_LIKE.match(name):
            readers.setdefault(name, 0)
            test_readers.setdefault(name, 0)
            if not reads:
                return
            if self.scope is None:
                read_at_import.add(name)
                if self.test_file:
                    test_readers[name] |= self.table.get((self.body, line), 0)
            else:
                if found:
                    readers[name] = readers[name] | found
                test_readers[name] |= EVERY_TEST if self.loader else self.ran(self.test_table, line)

    def visit_Name(self, node):
        self.note(node.id, node.lineno, isinstance(node.ctx, ast.Load))

    def visit_Attribute(self, node):
        reads = isinstance(node.ctx, ast.Load)
        self.note(node.attr, node.end_lineno, reads)
        self.note(node.attr, node.lineno, reads)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        # `from module import NAME [as other]` reads NAME and keeps what it read.
        for alias in node.names:
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                self.note(alias.name, line)

    def visit_Constant(self, node):
        # getattr(x, "NAME"), "NAME" in vars(x) and patch("module.NAME") read it too.
        if isinstance(node.value, str) and len(node.value) < 200:
            for name in set(node.value.split(".")):
                if name in constant_names or CONSTANT_LIKE.match(name):
                    for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                        self.note(name, line)

    def scoped(self, node, first):
        saved, self.scope = self.scope, first
        self.generic_visit(node)
        self.scope = saved

    def visit_FunctionDef(self, node):
        for extra in node.decorator_list + node.args.defaults + [d for d in node.args.kw_defaults if d]:
            self.visit(extra)
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        saved, self.scope = self.scope, first
        saved_loader = self.loader
        # A function that imports modules on demand runs whenever a module is
        # first needed, which depends on what ran before: what it reads is read
        # by every test.
        if any(
            isinstance(inner, (ast.Name, ast.Attribute))
            and (inner.id if isinstance(inner, ast.Name) else inner.attr) in IMPORTERS
            for inner in ast.walk(node)
        ) or (self.scope_depth == 0 and node.name == "__getattr__"):
            self.loader = True
            loaders.setdefault(self.path, []).append(first)
        self.scope_depth += 1
        for statement in node.body:
            self.visit(statement)
        self.scope_depth -= 1
        self.scope = saved
        self.loader = saved_loader

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        # Defaults are evaluated where the lambda is written.
        for extra in node.args.defaults + [d for d in node.args.kw_defaults if d]:
            self.visit(extra)
        saved, self.scope = self.scope, node.lineno
        self.visit(node.body)
        self.scope = saved

    def visit_GeneratorExp(self, node):
        # So is the first iterable of a generator expression.
        self.visit(node.generators[0].iter)
        saved, self.scope = self.scope, node.lineno
        self.visit(node.elt)
        for index, generator in enumerate(node.generators):
            self.visit(generator.target)
            if index:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        self.scope = saved

    def visit_ClassDef(self, node):
        saved, self.scope = self.scope, None
        saved_body = self.body
        for extra in node.decorator_list + node.bases + [keyword.value for keyword in node.keywords]:
            self.visit(extra)
        self.body = min([node.lineno] + [d.lineno for d in node.decorator_list])
        for statement in node.body:
            self.visit(statement)
        self.scope = saved
        self.body = saved_body


for path, tree in sources.items():
    Visitor(cov_file(path), family_of(path), tcov_file(path), path.startswith("tests/"), path).visit(tree)

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
if tcov_index is not None:
    open(os.path.join(out_dir, "treaders.bin"), "wb").write(pack(test_readers))
json.dump({"classes": class_statements, "modules": module_statements, "read_at_import": sorted(read_at_import),
           "loaders": {path: sorted(set(firsts)) for path, firsts in sorted(loaders.items())}},
          open(os.path.join(out_dir, "constants.json"), "w"), separators=(",", ":"), sort_keys=True)
json.dump(classes, open(os.path.join(out_dir, "classes.json"), "w"), separators=(",", ":"), sort_keys=True)
json.dump({"affected": affected, "tops": {k: sorted(v) for k, v in top_names.items()}},
          open(os.path.join(out_dir, "families.json"), "w"), separators=(",", ":"), sort_keys=True)
print(f"uses {len(uses)} classes, mentions {len(mentions)} names, "
      f"{sum(len(v) for v in classes.values())} classes in {len(classes)} files, {len(affected)} dialect families")
