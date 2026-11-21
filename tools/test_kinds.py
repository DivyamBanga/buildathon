"""Checks for the rules about kinds of change that one test run cannot settle.

    python tools/test_kinds.py [PINNED_SQLGLOT_DIR]

The first part needs nothing but this repository. The second part analyses the
seed patches in tools/kinds_seeds/ (each one was a commit for which the selector
left out a test that pytest fails) against the pinned tree; it runs when that
tree is found (the argument, or /sqlglot) and says so when it is not.
"""
import os
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from selector import analyze, pydiff  # noqa: E402
from selector.covmap import CovMap  # noqa: E402

MAPS = os.path.join(HERE, "..", "maps")
checks = 0


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        raise SystemExit(f"FAIL {label}: got {got!r}, want {want!r}")


# ---- E: a new test or helper may store to locals and self only -------------

TESTS = textwrap.dedent('''\
    import unittest
    from helpers import check


    def helper(x):
        return x


    class TestA(unittest.TestCase):
        def test_one(self):
            self.assertEqual(helper(1), 1)
            self.assertTrue(self.fixture_value)
''')
KNOWN = {"TestA::test_one"}


def added(addition, reserved=()):
    return pydiff.only_new_definitions(TESTS, TESTS + addition, KNOWN, reserved)


def effects(new_text, old_text=TESTS):
    return pydiff.new_global_effects(old_text, new_text)


METHOD = "\n    def test_two(self):\n{}\n"
expect("E: self attribute", added(METHOD.format("        self.x = 1\n        y = {}\n        y['a'] = 1")), False)  # subscript
expect("E: self attribute alone", added(METHOD.format("        self.x = 1\n        z = 2")), True)
expect("E: locals alone", added(METHOD.format("        z = [1]\n        for i in z:\n            w = i")), True)
for label, body in (
    ("class attribute", "        TestA.LIMIT = 3"),
    ("module attribute", "        unittest.foo = 3"),
    ("imported class attribute", "        from sqlglot.generators.duckdb import DuckDBGenerator\n        DuckDBGenerator.LIMIT_FETCH = 'FETCH'"),
    ("subscript", "        helper.cache['a'] = 1"),
    ("augmented attribute", "        helper.count += 1"),
    ("del attribute", "        del helper.count"),
    ("del subscript", "        del helper.cache['a']"),
    ("setattr", "        setattr(helper, 'a', 1)"),
    ("delattr", "        delattr(helper, 'a')"),
    ("__setattr__", "        object.__setattr__(helper, 'a', 1)"),
    ("global", "        global helper\n        helper = None"),
    ("nested nonlocal", "        n = 1\n        def inner():\n            nonlocal n\n            n = 2\n        inner()"),
    ("nested attribute store", "        def inner(o):\n            o.a = 1\n        inner(helper)"),
    ("store on self attribute", "        self.x.y = 1"),
    ("store in a with", "        with open('f') as helper.handle:\n            pass"),
    ("store in a for", "        for helper.x in range(2):\n            pass"),
):
    text = TESTS + METHOD.format(body)
    expect("E: new test, " + label + " (accepted?)", added(METHOD.format(body)), False)
    expect("E: new test, " + label + " (flagged?)", effects(text), True)
expect("E: new helper function, module store", effects(TESTS + "\n\ndef other(y):\n    helper.cache = y\n"), True)
expect("E: new helper function, locals", effects(TESTS + "\n\ndef other(y):\n    y.append(1)\n    return y\n"), False)
expect("E: module level store", effects(TESTS + "\nunittest.foo = 1\n"), True)
expect("E: class level store", effects(TESTS + "\nTestA.extra = 1\n"), True)
expect("E: module level name", effects(TESTS + "\nVALUE = 1\n"), False)
expect("E: self attribute is not flagged", effects(TESTS + METHOD.format("        self.x = 1")), False)
# A store that was already there is not new; one added to an existing test is.
BASE_STORES = TESTS + "\n\ndef setup_thing():\n    helper.cache = 1\n"
expect("E: existing store unchanged", effects(BASE_STORES + "\n\ndef other():\n    return 1\n", BASE_STORES), False)
expect("E: existing store, new copy", effects(BASE_STORES + "\n\ndef other():\n    helper.cache = 1\n", BASE_STORES), True)
expect("E: store added to an existing test", effects(TESTS.replace("self.assertEqual(helper(1), 1)", "helper.cache = 1")), True)
expect("E: text that does not parse", effects("def (:\n"), False)
expect("E: new file, store", effects("import os\nos.environ['A'] = '1'\n", ""), True)
expect("E: new file, plain", effects("import os\n\n\ndef test_a():\n    x = {}\n    x['a'] = 1\n    assert x\n", ""), True)  # subscript on a local: conservative
expect("E: new file, really plain", effects("import os\n\n\ndef test_a():\n    x = 1\n    assert x\n", ""), False)
expect("E: harmless new module with a store", pydiff.harmless_new_test_module(
    "import os\n\n\ndef test_a():\n    os.environ['A'] = '1'\n"), False)
expect("E: harmless new module", pydiff.harmless_new_test_module(
    "import os\n\n\ndef test_a():\n    x = os.sep\n    assert x\n"), True)

# ---- F: a new name must not change what existing code finds ---------------

expect("F: new global is a builtin", added("\n\ndef set(*args):\n    return {'x'}\n"), False)
expect("F: new global is a builtin (len)", added("\n\ndef len(x):\n    return 0\n"), False)
expect("F: new class is a builtin", added("\n\nclass dict:\n    pass\n"), False)
expect("F: new global loaded by old code", added("\n\ndef fixture_value():\n    return 1\n"), True)  # an attribute, not a global
expect("F: new global the old module loads", pydiff.only_new_definitions(
    TESTS + "\n\ndef use():\n    return later(1)\n", TESTS + "\n\ndef use():\n    return later(1)\n\n\ndef later(x):\n    return x\n",
    KNOWN), False)
expect("F: new global, fresh name", added("\n\ndef fresh_helper(x):\n    return x\n"), True)
expect("F: new test named like an attribute the old code reads", added(METHOD.format("        pass").replace("test_two", "fixture_value")), False)
expect("F: new test, read as attribute by old code", pydiff.only_new_definitions(
    TESTS + "\n    def test_a(self):\n        self.test_two()\n",
    TESTS + "\n    def test_a(self):\n        self.test_two()\n\n    def test_two(self):\n        pass\n", KNOWN), False)
expect("F: new test with the name of a test elsewhere", added(METHOD.format("        pass"), reserved={"test_two"}), False)
expect("F: new test, fresh name", added(METHOD.format("        pass"), reserved={"test_other"}), True)
expect("F: new test, TestCase name", added("\n    def run(self):\n        pass\n"), False)
expect("F: new method defined elsewhere in the module", pydiff.only_new_definitions(
    TESTS + "\n\nclass Base:\n    def test_shared(self):\n        pass\n",
    TESTS + "\n\nclass Base:\n    def test_shared(self):\n        pass\n\n    def test_new(self):\n        pass\n", KNOWN),
    True)
expect("F: new method named like another class's method", pydiff.only_new_definitions(
    TESTS + "\n\nclass Base:\n    def test_shared(self):\n        pass\n",
    TESTS.replace("self.assertTrue(self.fixture_value)", "self.assertTrue(self.fixture_value)\n\n    def test_shared(self):\n        pass")
    + "\n\nclass Base:\n    def test_shared(self):\n        pass\n", KNOWN), False)

# ---- D: a path in more than one section ------------------------------------


def section(old, new, body="@@ -1 +1 @@\n-a\n+b\n"):
    return f"diff --git a/{old} b/{new}\n--- a/{old}\n+++ b/{new}\n{body}"


def plan_of(text, root="/nonexistent"):
    return analyze.analyze("x", text, CovMap(MAPS), root, frozenset())


two = section("sqlglot/a.py", "sqlglot/a.py") + section("sqlglot/b.py", "sqlglot/b.py") + section("sqlglot/a.py", "sqlglot/a.py")
expect("D: same file twice", plan_of(two).select_all, True)
expect("D: note", any("more than one section" in note for note in plan_of(two).notes), True)
rename_edit = section("sqlglot/a.py", "sqlglot/c.py", "") + section("sqlglot/c.py", "sqlglot/c.py")
expect("D: renamed and edited", plan_of(rename_edit).select_all, True)
edit_then_rename = section("sqlglot/c.py", "sqlglot/c.py") + section("sqlglot/c.py", "sqlglot/d.py", "")
expect("D: edited and renamed", plan_of(edit_then_rename).select_all, True)
deleted_created = (
    "diff --git a/sqlglot/a.py b/sqlglot/a.py\ndeleted file mode 100644\n--- a/sqlglot/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
    "diff --git a/sqlglot/a.py b/sqlglot/a.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/a.py\n@@ -0,0 +1 @@\n+b\n"
)
expect("D: deleted and created", plan_of(deleted_created).select_all, True)
swap = section("sqlglot/a.py", "sqlglot/b.py", "") + section("sqlglot/b.py", "sqlglot/a.py", "")
expect("D: swapped names", plan_of(swap).select_all, True)
expect("D: dotted path", plan_of(section("sqlglot/./a.py", "sqlglot/./a.py") + section("sqlglot/a.py", "sqlglot/a.py")).select_all, True)
distinct = section("sqlglot/a.py", "sqlglot/a.py") + section("sqlglot/b.py", "sqlglot/b.py")
expect("D: two files are not repeated", any("more than one section" in note for note in plan_of(distinct).notes), False)

# ---- the seed patches --------------------------------------------------------

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/sqlglot"
if not os.path.isdir(os.path.join(ROOT, "sqlglot")):
    print(f"{checks} checks passed; the seed patches were not analysed (no pinned tree at {ROOT})")
    raise SystemExit(0)

covmap = CovMap(MAPS)
ID = {test_id: bit for bit, test_id in enumerate(covmap.ids)}


def seed(name):
    with open(os.path.join(HERE, "kinds_seeds", name + ".patch"), encoding="utf-8", newline="") as handle:
        return analyze.analyze(name, handle.read(), covmap, ROOT, frozenset())


def selected(plan, test_id):
    return plan.select_all or bool(plan.candidates >> ID[test_id] & 1)


for name in ("D_two_sections_a", "D_two_sections_b", "D_two_sections_c"):
    expect("seed " + name, seed(name).select_all, True)

plan = seed("C_pytest_mark")
TIME = "tests/test_time.py::TestTime::test_format_time"
expect("seed C: the test is a candidate", bool(plan.candidates >> ID[TIME] & 1), True)
expect("seed C: and is not run", bool(plan.unverified >> ID[TIME] & 1), True)
expect("seed C: every test of the file", plan.candidates & covmap.tests_of_file("tests/test_time.py"), covmap.tests_of_file("tests/test_time.py"))


def comment_patch(path, comment):
    """A patch that puts one comment line first in a file of the pinned tree."""
    import difflib

    with open(os.path.join(ROOT, path), encoding="utf-8", newline="") as handle:
        old = handle.read().splitlines(keepends=True)
    new = [comment + "\n"] + old
    body = "".join(difflib.unified_diff(old, new, "a/" + path, "b/" + path, n=3))
    return f"diff --git a/{path} b/{path}\n" + body


def plan_for(text):
    return analyze.analyze("x", text, covmap, ROOT, frozenset())


plan = plan_for(comment_patch("tests/test_time.py", "# pytest only"))
expect("C: comment that mentions pytest", plan.unverified & covmap.tests_of_file("tests/test_time.py") == covmap.tests_of_file("tests/test_time.py") != 0, True)
plan = plan_for(comment_patch("tests/test_time.py", "# nothing to see"))
expect("C: comment that does not", (plan.candidates, plan.select_all), (0, False))
plan = plan_for(comment_patch("tests/helpers.py", "# Pytest fixtures live elsewhere"))
expect("C: helper module, its importers", plan.unverified != 0 and plan.unverified == covmap.file_any("tests/helpers.py") | covmap.tests_of_file("tests/helpers.py"), True)
plan = plan_for(comment_patch("sqlglot/helper.py", "# pytest"))
expect("C: not a test file", (plan.candidates, plan.unverified), (0, 0))

plan = seed("E_pollution")
expect("seed E: everything", plan.select_all, True)

plan = seed("F_shadow_builtin")
expect("seed F: not 'only new tests'", any("only new tests" in note for note in plan.notes), False)
expect("seed F: the shadowed test is a candidate", selected(plan, "tests/test_helper.py::TestHelper::test_tsort"), True)

TYPING = "tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata"
for name in sorted(os.listdir(os.path.join(HERE, "kinds_seeds"))):
    if name.startswith("B_"):
        expect("seed " + name[:-6], selected(seed(name[:-6]), TYPING), True)


def new_file_patch(path, text):
    lines = text.splitlines()
    body = "".join("+" + line + "\n" for line in lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + body)


def deleted_file_patch(path):
    with open(os.path.join(ROOT, path), encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines()
    body = "".join("-" + line + "\n" for line in lines)
    return (f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n--- a/{path}\n+++ /dev/null\n"
            f"@@ -1,{len(lines)} +0,0 @@\n" + body)


everything = covmap.all
plan = plan_for(new_file_patch("tests/test_new_ok.py", "import unittest\n\n\nclass TestNew(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n"))
expect("B: a harmless new test file keeps its own rule", (plan.candidates, plan.select_all), (0, False))
plan = plan_for(new_file_patch("tests/test_new_bad.py", "import os\nos.environ['A'] = '1'\n"))
expect("B: a new test file that does more is not harmless", plan.select_all, True)
plan = plan_for(new_file_patch("sqlglot/typing/zz_new.py", "X = 1\n"))
expect("B: a new module next to the ones tests/test_typing.py reads", bool(plan.candidates >> ID[TYPING] & 1), True)
expect("B: and the plan is made in a clean child", plan.needs_clean, True)
plan = plan_for(new_file_patch("json.py", "X = 1\n"))
expect("B: a root module that shadows the standard one, every test", plan.candidates, everything)
expect("B: outside sqlglot/ and tests/ they are not run", plan.unverified, everything)
plan = plan_for(deleted_file_patch("sqlglot/typing/hive.py"))
expect("B: a removed module", bool(plan.candidates >> ID[TYPING] & 1), True)
covmap.entry_listers("x", ROOT)
if covmap._dirs["names"] is not None:  # with the names of the import attempts; without them every module name counts
    plan = plan_for(new_file_patch("benchmarks/zz_new.py", "X = 1\n"))
    expect("B: a file nobody lists, and nobody imports", (plan.candidates, plan.select_all), (0, False))
    plan = plan_for(new_file_patch("docs/zz_note.txt", "x" + chr(10)))
    expect("B: a note in a new directory", (plan.candidates, plan.select_all), (0, False))

print(f"{checks} checks passed")
