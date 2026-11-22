"""Generate commits that add or remove names without editing code in place (a lab tool, not in the image).

    python make_additive.py /sqlglot /out/gen  [names.json]

Writes /out/gen/commits/<id>.patch (unified diffs against the pinned tree) and
/out/gen/manifest.json (id -> kind, file, description). The mix: an unused
constant, function or class appended to a module; a helper added and called; a
method, class attribute, property, dunder, override added to a class of each
family; imports added and removed; unused and used functions, methods and
imports removed; names that shadow a builtin; new expression, dialect and enum
classes; entries of the lazy tables. Names the pinned code never reads are
found with names.json so that the "unused" ones really are.
"""
import ast
import difflib
import json
import os
import sys

ROOT = sys.argv[1]
OUT = sys.argv[2]
NAMES = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else None
patches: dict = {}
manifest: dict = {}


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8", newline="") as handle:
        return handle.read()


def emit(ident, kind, path, new_text, description, old_text=None):
    old_text = read(path) if old_text is None else old_text
    if new_text == old_text:
        print("NO CHANGE", ident)
        return
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True), new_text.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3
    )
    body = "".join(diff)
    patches[ident] = f"diff --git a/{path} b/{path}\n" + body
    manifest[ident] = {"kind": kind, "file": path, "description": description}


def append(text, snippet):
    return text.rstrip("\n") + "\n\n\n" + snippet.strip("\n") + "\n"


def class_span(text, qualname):
    """(first line index, index after the last line of the body) of a class, by dotted name."""
    lines = text.split("\n")
    start, indent = 0, -1
    for part in qualname.split("."):
        found = None
        for index in range(start, len(lines)):
            line = lines[index]
            stripped = line.lstrip()
            current = len(line) - len(stripped)
            if indent >= 0 and stripped and current <= indent and index > start:
                break
            if stripped.startswith(f"class {part}") and stripped[len(f"class {part}")] in "(:" and current > indent:
                found = index
                break
        if found is None:
            raise KeyError(qualname)
        start = found
        indent = len(lines[found]) - len(lines[found].lstrip())
    end = start + 1
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        end = index + 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end, indent


def add_to_class(text, qualname, snippet, at_end=True):
    start, end, indent = class_span(text, qualname)
    lines = text.split("\n")
    pad = " " * (indent + 4)
    block = [(pad + line if line.strip() else line) for line in snippet.strip("\n").split("\n")]
    if at_end:
        lines[end:end] = [""] + block
    else:
        # after the class header and its docstring/first statement block: right after the opening line
        index = start + 1
        while not lines[index - 1].rstrip().endswith(":"):
            index += 1
        lines[index:index] = block + [""]
    return "\n".join(lines)


def replace_once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)


def remove_lines(text, first_line_text):
    lines = text.split("\n")
    index = lines.index(first_line_text)
    del lines[index]
    return "\n".join(lines)


def remove_def(text, name, top=True, indent=0):
    """Remove `def name` (with decorators and the body) at the given indent."""
    lines = text.split("\n")
    pad = " " * indent
    for index, line in enumerate(lines):
        if line.startswith(f"{pad}def {name}(") or line.startswith(f"{pad}async def {name}("):
            start = index
            while start > 0 and lines[start - 1].startswith(pad + "@"):
                start -= 1
            end = index + 1
            while end < len(lines) and (not lines[end].strip() or lines[end].startswith(pad + " ") or lines[end].startswith(pad + ")")):
                end += 1
            while end > index + 1 and not lines[end - 1].strip():
                end -= 1
            # keep the blank lines that separated it from the next definition
            del lines[start:end]
            while start < len(lines) and start > 0 and not lines[start].strip() and not lines[start - 1].strip() and (
                start + 1 < len(lines) and not lines[start + 1].strip() and start + 2 < len(lines) and not lines[start + 2].strip()
            ):
                del lines[start]
            return "\n".join(lines)
    raise KeyError(name)


CONST = "ZZ_ADDED = 1"
FUNC = "def zz_added(value):\n    return value"
PLAIN = "class ZzAdded:\n    VALUE = 1\n\n    def method(self):\n        return self.VALUE"

# ---- appended names -------------------------------------------------------------------------------------------
MODULES = {
    "helper": "sqlglot/helper.py",
    "expcore": "sqlglot/expressions/core.py",
    "expquery": "sqlglot/expressions/query.py",
    "dialect": "sqlglot/dialects/dialect.py",
    "snowflake": "sqlglot/dialects/snowflake.py",
    "duckdb": "sqlglot/dialects/duckdb.py",
    "generator": "sqlglot/generator.py",
    "gduck": "sqlglot/generators/duckdb.py",
    "parser": "sqlglot/parser.py",
    "pduck": "sqlglot/parsers/duckdb.py",
    "simplify": "sqlglot/optimizer/simplify.py",
    "scope": "sqlglot/optimizer/scope.py",
    "executor": "sqlglot/executor/python.py",
    "env": "sqlglot/executor/env.py",
    "tokens": "sqlglot/tokens.py",
    "transforms": "sqlglot/transforms.py",
    "schema": "sqlglot/schema.py",
    "time": "sqlglot/time.py",
    "lineage": "sqlglot/lineage.py",
    "errors": "sqlglot/errors.py",
}
for key, path in MODULES.items():
    text = read(path)
    emit(f"a_const_{key}", "const", path, append(text, CONST), "unused constant appended")
for key in ("helper", "dialect", "generator", "parser", "simplify", "executor", "env", "expquery", "duckdb", "gduck", "pduck", "transforms"):
    path = MODULES[key]
    emit(f"b_func_{key}", "function", path, append(read(path), FUNC), "unused function appended")
for key in ("helper", "dialect", "parser", "simplify", "scope", "gduck", "env"):
    path = MODULES[key]
    emit(f"c_class_{key}", "class", path, append(read(path), PLAIN), "unused plain class appended")
emit("a_private_helper", "const", MODULES["helper"], append(read(MODULES["helper"]), "_zz_added = 1"), "unused private constant")
emit("a_two_names", "const", MODULES["time"], append(read(MODULES["time"]), "ZZ_ONE = 1\nZZ_TWO = 'two'"), "two unused constants")
emit("a_annot", "const", MODULES["helper"], append(read(MODULES["helper"]), "ZZ_TYPED: int = 3"), "annotated unused constant")

# ---- a helper added and called ------------------------------------------------------------------------------------
text = read(MODULES["helper"])
new = replace_once(text, "    return sep.join(arg for arg in args if arg)\n", "    return _zz_join(sep, args)\n")
new = append(new, "def _zz_join(sep, args):\n    return sep.join(arg for arg in args if arg)")
emit("d_helper_called_csv", "call", MODULES["helper"], new, "new helper called from csv() (changed caller)")
new = replace_once(text, "    return next(i for i in it)\n", "    return _zz_first(it)\n")
new = append(new, "def _zz_first(it):\n    return next(i for i in it)")
emit("d_helper_called_first", "call", MODULES["helper"], new, "new helper called from first()")
text = read(MODULES["generator"])
new = replace_once(text, "    def sep(self, sep: str = \" \") -> str:\n", "    def _zz_sep(self, sep):\n        return f\"{sep}\\n\" if self.pretty else sep\n\n    def sep(self, sep: str = \" \") -> str:\n")
emit("d_method_added_uncalled_before_sep", "method", MODULES["generator"], new, "method inserted in the middle of the generator, nothing calls it")
text = read(MODULES["helper"])
new = replace_once(text, "def first(it: Iterable[T]) -> T:\n", "def _zz_mid(value):\n    return value\n\n\ndef first(it: Iterable[T]) -> T:\n")
emit("d_func_inserted_mid", "function", MODULES["helper"], new, "unused function inserted before first()")

# ---- methods, attributes, properties on classes of each family ---------------------------------------------------------
FAMILIES = {
    "generator": ("sqlglot/generator.py", "Generator"),
    "gduck": ("sqlglot/generators/duckdb.py", "DuckDBGenerator"),
    "parser": ("sqlglot/parser.py", "Parser"),
    "pduck": ("sqlglot/parsers/duckdb.py", "DuckDBParser"),
    "tokens": ("sqlglot/tokens.py", "Tokenizer"),
    "dialect": ("sqlglot/dialects/dialect.py", "Dialect"),
    "duckdb": ("sqlglot/dialects/duckdb.py", "DuckDB"),
    "ducktok": ("sqlglot/dialects/duckdb.py", "DuckDB.Tokenizer"),
    "query": ("sqlglot/expressions/query.py", "Select"),
    "core": ("sqlglot/expressions/core.py", "Expr"),
    "scope": ("sqlglot/optimizer/scope.py", "Scope"),
    "pyexec": ("sqlglot/executor/python.py", "PythonExecutor"),
}
for key, (path, cls) in FAMILIES.items():
    text = read(path)
    emit(f"e_method_{key}", "method", path, add_to_class(text, cls, "def zz_helper(self):\n    return 1"), f"plain method added to {cls}")
    emit(f"f_attr_{key}", "attr", path, add_to_class(text, cls, "ZZ_FLAG = True"), f"plain class attribute added to {cls}")
for key in ("generator", "parser", "query"):
    path, cls = FAMILIES[key]
    text = read(path)
    emit(f"e_property_{key}", "property", path, add_to_class(text, cls, "@property\ndef zz_prop(self):\n    return 1"), f"property added to {cls}")
# dispatch-by-name and the like
path, cls = FAMILIES["generator"]
text = read(path)
emit("e_sql_unknown_key", "dispatch", path, add_to_class(text, cls, "def zzadded_sql(self, expression):\n    return 'ZZ'"), "a *_sql method no expression class matches")
emit("e_sql_real_key", "dispatch", path, add_to_class(text, cls, "def bitwisenot_sql(self, expression):\n    return 'ZZ'"), "a *_sql method for an existing key (replaces generation)")
path, cls = FAMILIES["gduck"]
emit("e_sql_real_key_duck", "dispatch", path, add_to_class(read(path), cls, "def bitwisenot_sql(self, expression):\n    return 'ZZ'"), "a *_sql method for an existing key in DuckDB")
path, cls = FAMILIES["parser"]
emit("e_parse_new", "method", path, add_to_class(read(path), cls, "def _parse_zz_added(self):\n    return None"), "a _parse_ method nothing calls")
path, cls = FAMILIES["core"]
emit("e_dunder_expr", "dunder", path, add_to_class(read(path), cls, "def __zz_added__(self):\n    return 1"), "a made-up dunder on Expr")
emit("e_dunder_real", "dunder", path, add_to_class(read(path), cls, "def __bool__(self):\n    return True"), "__bool__ added on Expr")
path, cls = FAMILIES["tokens"]
emit("e_dunder_tok", "dunder", "sqlglot/tokenizer_core.py", add_to_class(read("sqlglot/tokenizer_core.py"), "Token", "def __len__(self):\n    return 0"), "__len__ added on Token")
path, cls = FAMILIES["generator"]
emit("e_override_sep", "override", path, add_to_class(read(path), "Generator", "def zz_sep_alias(self, sep=' '):\n    return self.sep(sep)"), "alias method calling an inherited one")
path = "sqlglot/generators/duckdb.py"
emit("e_override_inherited", "override", path, add_to_class(read(path), "DuckDBGenerator", "def sep(self, sep=' '):\n    return ' '"), "inherited method overridden in DuckDBGenerator")
path = "sqlglot/dialects/duckdb.py"
emit("f_attr_shadow_flag", "attr", path, add_to_class(read(path), "DuckDB", "NORMALIZE_FUNCTIONS = False"), "an inherited flag set in DuckDB")
emit("f_attr_metaclass_walk", "attr", path, add_to_class(read(path), "DuckDB", "INVERSE_TIME_MAPPING = {'zz': 'zz'}"), "an attribute the dialect metaclass reads from the class dict")
emit("f_attr_tokenizer_strings", "attr", path, add_to_class(read(path), "DuckDB.Tokenizer", "BYTE_STRINGS = [\"b'\"]"), "an attribute Tokenizer.__init_subclass__ reads")
emit("f_attr_nested_parser", "attr", "sqlglot/dialects/duckdb.py", add_to_class(read("sqlglot/dialects/duckdb.py"), "DuckDB", "Parser = None"), "the nested Parser name the metaclass reads")
emit("f_attr_exprkey", "attr", "sqlglot/expressions/query.py", add_to_class(read("sqlglot/expressions/query.py"), "Select", "key = 'zzselect'"), "key set by hand on an expression class")

# ---- imports ----------------------------------------------------------------------------------------------------------
text = read(MODULES["helper"])
emit("g_import_stdlib_new", "import", MODULES["helper"], replace_once(text, "import sys\n", "import sys\nimport textwrap\n"), "an import of a stdlib module nothing else binds")
emit("g_import_known", "import", MODULES["helper"], replace_once(text, "import sys\n", "import sys\nimport json\n"), "an import of a module other namespaces bind")
emit("g_import_from", "import", MODULES["dialect"], replace_once(read(MODULES["dialect"]), "from enum import Enum, auto\n", "from enum import Enum, auto, IntEnum\n"), "a name added to a from-import")
emit("g_import_used_removed", "import", MODULES["helper"], remove_lines(text, "from difflib import get_close_matches"), "an import removed that a function uses (must fail)")
emit("g_import_future_removed", "import", MODULES["helper"], remove_lines(text, "from copy import copy"), "the import of copy removed (object_to_dict uses it)")

# find imports that nothing reads
if NAMES:
    named = set(NAMES["named"])
    reads = NAMES["globals"]
    unused_imports = []
    unused_funcs = []
    for directory, _, files in os.walk(os.path.join(ROOT, "sqlglot")):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            relative = os.path.relpath(os.path.join(directory, name), ROOT).replace(os.sep, "/")
            source = read(relative)
            tree = ast.parse(source)
            lines = source.split("\n")
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)) and len(node.names) == 1 and node.end_lineno == node.lineno:
                    alias = node.names[0]
                    bound = (alias.asname or alias.name).split(".")[0]
                    if bound != "*" and bound not in named and bound not in reads.get(relative, ()) and not isinstance(node, ast.ImportFrom) or (
                        isinstance(node, ast.ImportFrom) and node.module != "__future__" and bound not in named and bound not in reads.get(relative, ())
                    ):
                        unused_imports.append((relative, node.lineno, lines[node.lineno - 1]))
                elif isinstance(node, ast.FunctionDef) and node.name not in named and node.name not in reads.get(relative, ()):
                    unused_funcs.append((relative, node.name))
    print("unused imports", len(unused_imports), "unused functions", len(unused_funcs))
    seen = set()
    count = 0
    for relative, lineno, line in unused_imports:
        if relative in seen or relative.startswith("sqlglot/dialects/__init__") or relative.endswith("__init__.py"):
            continue
        seen.add(relative)
        text = read(relative)
        emit(f"h_import_unused_{count}", "remove", relative, remove_lines(text, line), f"unused import removed: {line.strip()}")
        count += 1
        if count >= 5:
            break
    seen = set()
    count = 0
    for relative, name in unused_funcs:
        if relative in seen:
            continue
        seen.add(relative)
        try:
            new = remove_def(read(relative), name)
        except KeyError:
            continue
        emit(f"h_func_unused_{count}", "remove", relative, new, f"unused function removed: {name}")
        count += 1
        if count >= 6:
            break

# ---- removal of things in use (must fail) ------------------------------------------------------------------------------
text = read(MODULES["generator"])
emit("i_method_used_removed", "remove", MODULES["generator"], remove_def(text, "sep", indent=4), "a method others call removed (must fail)")
emit("i_func_used_removed", "remove", MODULES["helper"], remove_def(read(MODULES["helper"]), "dict_depth"), "a function others call removed (must fail)")
emit("i_const_used_removed", "remove", MODULES["helper"], remove_lines(read(MODULES["helper"]), "CAMEL_CASE_PATTERN = re.compile(\"(?<!^)(?=[A-Z])\")") if "CAMEL_CASE_PATTERN = re.compile(\"(?<!^)(?=[A-Z])\")" in read(MODULES["helper"]) else read(MODULES["helper"]), "a module constant others read removed (must fail)")

# ---- names that shadow ------------------------------------------------------------------------------------------------
emit("j_shadow_builtin_max", "shadow", MODULES["helper"], append(read(MODULES["helper"]), "def max(*args):\n    return 0"), "a module-level max() shadowing the builtin the module calls (if it does)")
emit("j_shadow_builtin_in_simplify", "shadow", MODULES["simplify"], append(read(MODULES["simplify"]), "def isinstance(value, kind):\n    return False"), "isinstance shadowed in a module that uses it")
emit("j_shadow_builtin_unused", "shadow", MODULES["time"], append(read(MODULES["time"]), "def zip(*args):\n    return []"), "zip shadowed in a module that may not use it")

# ---- registering classes ---------------------------------------------------------------------------------------------------
emit("k_expr_func", "register", "sqlglot/expressions/string.py", append(read("sqlglot/expressions/string.py"), "class ZzAddedFunc(Func):\n    arg_types = {\"this\": True}"), "a new Func subclass registers itself in EXPR_CLASSES and FUNCTION_BY_NAME")
emit("k_expr_plain", "register", "sqlglot/expressions/query.py", append(read("sqlglot/expressions/query.py"), "class ZzAddedExpr(Expr):\n    arg_types = {\"this\": True}"), "a new Expr subclass")
emit("k_dialect_class", "register", MODULES["duckdb"], append(read(MODULES["duckdb"]), "class ZzDialect(Dialect):\n    pass"), "a new dialect class registers itself")
emit("k_enum_member", "register", MODULES["dialect"], replace_once(read(MODULES["dialect"]), "class Dialects(str, Enum):\n", "class Dialects(str, Enum):\n    ZZ = \"zz\"\n"), "a new member of the Dialects enum")
emit("k_enum_plain", "register", "sqlglot/trie.py", append(read("sqlglot/trie.py"), "class ZzKind(Enum):\n    A = auto()"), "a new enum class nothing uses")
text = read("sqlglot/dialects/__init__.py")
emit("k_lazy_entry", "lazy", "sqlglot/dialects/__init__.py", text.replace('    "DuckDB",\n', '    "DuckDB",\n    "ZzLazy",\n', 1), "a new entry in the lazy attribute table")
emit("k_lazy_name_added", "lazy", "sqlglot/dialects/__init__.py", append(text, "ZZ_ADDED = 1"), "a constant added to the lazy package")
text = read("sqlglot/optimizer/__init__.py")
emit("k_optimizer_init", "lazy", "sqlglot/optimizer/__init__.py", append(text, "ZZ_ADDED = 1"), "a constant added to the optimizer package")

# ---- combinations -----------------------------------------------------------------------------------------------------------
text = read(MODULES["helper"])
emit("l_add_two_files", "multi", MODULES["helper"], append(text, CONST), "constant in helper (paired with a second file below)")
text = append(read(MODULES["helper"]), CONST + "\n\n\n" + FUNC)
emit("l_const_and_func", "multi", MODULES["helper"], text, "constant and function appended together")
text = read(MODULES["generator"])
emit("l_method_and_attr", "multi", MODULES["generator"], add_to_class(add_to_class(text, "Generator", "ZZ_FLAG = True"), "Generator", "def zz_helper(self):\n    return self.ZZ_FLAG"), "attribute and a method reading it")
text = read(MODULES["helper"])
emit("l_add_and_edit", "multi", MODULES["helper"], replace_once(append(text, CONST), "    return next(i for i in it)\n", "    return next(iter(it))\n"), "an unused constant and an in-place edit of first()")

emit("k_lazy_optimizer_entry", "lazy", "sqlglot/optimizer/__init__.py", replace_once(read("sqlglot/optimizer/__init__.py"), '    "Scope": ("sqlglot.optimizer.scope", "Scope"),\n', '    "Scope": ("sqlglot.optimizer.scope", "Scope"),\n    "ZzLazy": ("sqlglot.optimizer.scope", "Scope"),\n'), "a new entry in the optimizer lazy table")

def emit_new(ident, kind, path, text, description):
    body = "".join(difflib.unified_diff([], text.splitlines(keepends=True), "/dev/null", f"b/{path}", n=3))
    patches[ident] = f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n" + body.replace("--- /dev/null", "--- /dev/null", 1)
    manifest[ident] = {"kind": kind, "file": path, "description": description}


emit_new("n_newmodule_plain", "newfile", "sqlglot/zz_new.py", "ZZ_NEW = 1\n\n\ndef zz_new():\n    return ZZ_NEW\n", "a new module nothing imports")
emit_new("n_newmodule_typing", "newfile", "sqlglot/typing/zz_new.py", "ZZ_NEW = 1\n", "a new module in the package test_typing enumerates")
emit_new("n_newtest_plain", "newfile", "tests/test_zz_new.py", "import unittest\n\n\nclass TestZz(unittest.TestCase):\n    def test_zz(self):\n        self.assertTrue(True)\n", "a new test file")

os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)
for ident, body in patches.items():
    with open(os.path.join(OUT, "commits", ident + ".patch"), "w", encoding="utf-8", newline="") as handle:
        handle.write(body)
with open(os.path.join(OUT, "manifest.json"), "w") as handle:
    json.dump(manifest, handle, indent=1, sort_keys=True)
print(len(patches), "patches")
