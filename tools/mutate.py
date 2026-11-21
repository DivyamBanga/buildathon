"""Generate patches against the pinned commit to validate the selector on.

These are development data only: they are labeled with a real pytest run
(tools/gt_run.py) and used to check that the selector never misses. Nothing
derived from them ships in the image.

Usage (inside the lab image):
    python mutate.py <out_dir> <count> <seed> [kind,kind,...]

Kinds: code fixture comment multi testcode delete docstring blank swap helper
"""
import ast
import difflib
import os
import random
import re
import sys

ROOT = "/sqlglot"
out_dir, count, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
kinds = sys.argv[4].split(",") if len(sys.argv) > 4 else None
rng = random.Random(seed)
os.makedirs(out_dir, exist_ok=True)


def walk(top, suffix):
    found = []
    for base, _dirs, files in os.walk(os.path.join(ROOT, top)):
        if "__pycache__" in base:
            continue
        for name in files:
            if name.endswith(suffix):
                found.append(os.path.relpath(os.path.join(base, name), ROOT))
    return sorted(found)


SOURCES = walk("sqlglot", ".py")
TESTS = [p for p in walk("tests", ".py") if os.path.basename(p).startswith("test_")]
FIXTURES = walk("tests/fixtures", ".sql")
_cache = {}


def read(path):
    if path not in _cache:
        _cache[path] = open(os.path.join(ROOT, path), encoding="utf-8").read()
    return _cache[path]


def weighted(paths):
    weights = [max(1, read(p).count("\n")) for p in paths]
    return rng.choices(paths, weights)[0]


def diff(path, old, new):
    lines = list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        f"a/{path}", f"b/{path}", n=3,
    ))
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + "".join(lines)


OPERATORS = [
    (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "),
    (r" > ", " >= "), (r" >= ", " > "), (r" is not ", " is "), (r" is None", " == None"),
    (r" is (?!not)", " == "), (r" and ", " or "), (r" or ", " and "),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\bnot ", ""),
    (r" \+ ", " - "), (r" - ", " + "), (r" in ", " not in "), (r" not in ", " in "),
    (r"(?<![\w.\"'])(\d+)(?![\w.\"'])", lambda m: str(int(m.group(1)) + 1)),
    (r"^(\s*)if (.+):\s*$", lambda m: f"{m.group(1)}if not ({m.group(2)}):"),
    (r"^(\s*)return (.+)$", lambda m: f"{m.group(1)}return None"),
    (r"\"([A-Za-z_]{3,})\"", lambda m: '"' + m.group(1) + 'X"'),
    (r"\bmin\(", "max("), (r"\bmax\(", "min("), (r"\[0\]", "[-1]"), (r"\[-1\]", "[0]"),
    (r"\.get\(([^(),]+)\)", lambda m: f"[{m.group(1)}]"),
    (r"\bany\(", "all("), (r"\ball\(", "any("),
    (r"copy=False", "copy=True"), (r"copy=True", "copy=False"),
]


def mutate_python_line(path, in_function_only=False):
    """One-line edit of a Python file that still compiles and changes the AST."""
    old = read(path)
    lines = old.split("\n")
    for _ in range(200):
        index = rng.randrange(len(lines))
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", '"""', "'''", "import ", "from ", "@")):
            continue
        pattern, replacement = rng.choice(OPERATORS)
        matches = list(re.finditer(pattern, line))
        if not matches:
            continue
        match = rng.choice(matches)
        text = replacement(match) if callable(replacement) else match.expand(replacement) if "\\" in replacement else replacement
        candidate = line[:match.start()] + text + line[match.end():]
        if candidate == line:
            continue
        new_lines = list(lines)
        new_lines[index] = candidate
        new = "\n".join(new_lines)
        try:
            if ast.dump(ast.parse(new)) == ast.dump(ast.parse(old)):
                continue
        except SyntaxError:
            continue
        return old, new
    return None


def mutate_fixture():
    path = rng.choice(FIXTURES)
    old = read(path)
    lines = old.split("\n")
    for _ in range(200):
        index = rng.randrange(len(lines))
        line = lines[index]
        if not line.strip() or line.startswith(("--", "#")):
            if rng.random() > 0.08:
                continue
        words = line.split(" ")
        action = rng.choice(["insert", "insert", "delete", "replace", "dup"])
        position = rng.randrange(len(words))
        if action == "insert":
            words.insert(position, rng.choice(["42", "x", "NULL", "0", "AS", "1 +", "NOT"]))
        elif action == "delete" and len(words) > 1:
            del words[position]
        elif action == "replace":
            words[position] = rng.choice(["y", "z", "1", "FOO", "b"])
        elif action == "dup":
            words.insert(position, words[position])
        candidate = " ".join(words)
        if candidate == line:
            continue
        new_lines = list(lines)
        new_lines[index] = candidate
        return path, old, "\n".join(new_lines)
    return None


COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# Intentionally not cached",
            "# Mirrors the behavior of the reference implementation", "# Keep in sync with the generator",
            "# TODO: revisit", "# type: ignore", "# noqa"]


def add_comment(path):
    old = read(path)
    lines = old.split("\n")
    for _ in range(50):
        index = rng.randrange(1, len(lines))
        target = lines[index]
        if not target.strip():
            continue
        indent = target[: len(target) - len(target.lstrip())]
        new_lines = lines[:index] + [indent + rng.choice(COMMENTS)] + lines[index:]
        new = "\n".join(new_lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return old, new
    return None


def function_statements(path):
    """(first line, last line) of simple statements inside functions."""
    tree = ast.parse(read(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, (ast.Assign, ast.AugAssign, ast.Expr, ast.Return)) and not (
                    isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant)
                ):
                    found.append((child.lineno, child.end_lineno, child))
    return found


def delete_statement(path):
    old = read(path)
    lines = old.split("\n")
    statements = function_statements(path)
    for _ in range(50):
        first, last, _node = rng.choice(statements)
        indent = lines[first - 1][: len(lines[first - 1]) - len(lines[first - 1].lstrip())]
        new = "\n".join(lines[: first - 1] + [indent + "pass"] + lines[last:])
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return old, new
    return None


def swap_statements(path):
    old = read(path)
    lines = old.split("\n")
    tree = ast.parse(old)
    bodies = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.If, ast.For, ast.While)):
            body = node.body
            for a, b in zip(body, body[1:]):
                if a.end_lineno + 1 == b.lineno and not isinstance(a, (ast.FunctionDef, ast.ClassDef)):
                    bodies.append((a, b))
    if not bodies:
        return None
    a, b = rng.choice(bodies)
    new_lines = lines[: a.lineno - 1] + lines[b.lineno - 1 : b.end_lineno] + lines[a.lineno - 1 : a.end_lineno] + lines[b.end_lineno :]
    new = "\n".join(new_lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return old, new


def edit_docstring(path):
    old = read(path)
    tree = ast.parse(old)
    lines = old.split("\n")
    docs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module)) and ast.get_docstring(node, clean=False):
            docs.append(node.body[0])
    if not docs:
        return None
    node = rng.choice(docs)
    index = rng.randrange(node.lineno, node.end_lineno + 1) - 1
    words = lines[index].split(" ")
    if len(words) < 2:
        return None
    words.insert(rng.randrange(1, len(words)), "really")
    lines[index] = " ".join(words)
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return old, new


def blank_lines(path):
    old = read(path)
    lines = old.split("\n")
    index = rng.randrange(1, len(lines))
    new = "\n".join(lines[:index] + [""] + lines[index:])
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return old, new


def make(kind):
    if kind == "code":
        path = weighted(SOURCES)
        result = mutate_python_line(path)
        return diff(path, *result) if result else None
    if kind == "testcode":
        path = weighted(TESTS)
        result = mutate_python_line(path)
        return diff(path, *result) if result else None
    if kind == "helper":
        path = rng.choice(["tests/helpers.py", "tests/dialects/test_dialect.py"])
        result = mutate_python_line(path)
        return diff(path, *result) if result else None
    if kind == "fixture":
        result = mutate_fixture()
        return diff(*result) if result else None
    if kind == "comment":
        path = weighted(SOURCES + TESTS[:3])
        result = add_comment(path)
        return diff(path, *result) if result else None
    if kind == "delete":
        path = weighted(SOURCES)
        result = delete_statement(path)
        return diff(path, *result) if result else None
    if kind == "swap":
        path = weighted(SOURCES)
        result = swap_statements(path)
        return diff(path, *result) if result else None
    if kind == "docstring":
        path = weighted(SOURCES)
        result = edit_docstring(path)
        return diff(path, *result) if result else None
    if kind == "blank":
        path = weighted(SOURCES)
        result = blank_lines(path)
        return diff(path, *result) if result else None
    if kind == "multi":
        parts, used = [], set()
        for sub in rng.sample(["code", "fixture", "comment", "comment", "code"], rng.choice([2, 3])):
            for _ in range(20):
                piece = make(sub)
                if piece:
                    path = piece.split("\n", 1)[0].split(" b/")[-1]
                    if path not in used:
                        used.add(path)
                        parts.append((path, piece))
                        break
        return "".join(piece for _path, piece in sorted(parts)) if len(parts) >= 2 else None
    raise SystemExit(f"unknown kind {kind}")


DEFAULT = (["code"] * 45 + ["fixture"] * 18 + ["comment"] * 6 + ["multi"] * 8 + ["testcode"] * 6
           + ["delete"] * 5 + ["swap"] * 4 + ["docstring"] * 3 + ["blank"] * 2 + ["helper"] * 3)
made = 0
manifest = []
while made < count:
    kind = rng.choice(kinds or DEFAULT)
    text = make(kind)
    if not text:
        continue
    name = f"m{seed:02d}{made:03d}"
    with open(os.path.join(out_dir, name + ".patch"), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    manifest.append(f"{name} {kind}")
    made += 1
with open(os.path.join(out_dir, f"manifest-{seed}.txt"), "w") as handle:
    handle.write("\n".join(manifest) + "\n")
print("wrote", made)
