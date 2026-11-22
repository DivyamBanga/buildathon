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
expect("E: new helper function, locals", effects(TESTS + "\n\ndef other(y):\n    z = [y]\n    return z\n"), False)
expect("E: new helper function, a call that mutates its argument", effects(TESTS + "\n\ndef other(y):\n    y.append(1)\n    return y\n"), True)
expect("E: module level store", effects(TESTS + "\nunittest.foo = 1\n"), True)
expect("E: class level store", effects(TESTS + "\nTestA.extra = 1\n"), True)
expect("E: module level name", effects(TESTS + "\nVALUE = 1\n"), False)
expect("E: self attribute is not flagged", effects(TESTS + METHOD.format("        self.x = 1")), False)
# A store that was already there is not new; one added to an existing test is.
BASE_STORES = TESTS + "\n\ndef setup_thing():\n    helper.cache = 1\n"
expect("E: existing store unchanged", effects(BASE_STORES + "\n\ndef other():\n    return 1\n", BASE_STORES), False)
expect("E: existing store, new copy", effects(BASE_STORES + "\n\ndef other():\n    helper.cache = 1\n", BASE_STORES), True)
expect("E: store added to an existing test", effects(TESTS.replace("self.assertEqual(helper(1), 1)", "helper.cache = 1")), True)
STORE_TEST = TESTS + "\n\ndef restore():\n    try:\n        pass\n    finally:\n        helper.mode = 'a'\n"
expect("E: existing store, another value", effects(STORE_TEST.replace("'a'", "'b'"), STORE_TEST), True)
expect("E: existing store, another value in a nested block", effects(STORE_TEST.replace("= 'a'", "= helper.other"), STORE_TEST), True)
expect("E: existing store, same statement", effects(STORE_TEST + "\n\ndef other():\n    return 1\n", STORE_TEST), False)
expect("E: text that does not parse", effects("def (:\n"), False)
expect("E: new file, store", effects("import os\nos.environ['A'] = '1'\n", ""), True)
expect("E: new file, plain", effects("import os\n\n\ndef test_a():\n    x = {}\n    x['a'] = 1\n    assert x\n", ""), True)  # subscript on a local: conservative
expect("E: new file, really plain", effects("import os\n\n\ndef test_a():\n    x = 1\n    assert x\n", ""), False)
expect("E: harmless new module with a store", pydiff.harmless_new_test_module(
    "import os\n\n\ndef test_a():\n    os.environ['A'] = '1'\n"), False)
expect("E: harmless new module", pydiff.harmless_new_test_module(
    "import os\n\n\ndef test_a():\n    x = os.sep\n    assert x\n"), True)

# module-level constants of a new test file: values built without calling anything, and re.compile of a constant
NEW_FILE = "import re\nimport unittest\n\n{}\n\n\nclass TestZ(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n"
for label, constant, want in (
    ("a number", "LIMIT = 3", True),
    ("a negative number", "LIMIT = -3", True),
    ("a string", "SQL = 'SELECT 1'", True),
    ("None", "CACHE = None", True),
    ("two names", "A = B = 'x'", True),
    ("a tuple of constants", "CASES = ('a', 1, (2.5, None))", True),
    ("a list of tuples", "CASES = [('a', 'b'), ('c', 'd')]", True),
    ("a dict of lists", "TABLE = {'a': [1, 2], ('b', 1): {'c': None}}", True),
    ("a set", "NAMES = {'a', 'b'}", True),
    ("a compiled pattern", "ATOM = re.compile(r'%(?:[mdHIMS]strict|[-:].|[\\w%])')", True),
    ("a compiled pattern with flags", "ATOM = re.compile('a', re.I | re.M)", True),
    ("a compiled pattern with a flags keyword", "ATOM = re.compile('a', flags=re.DOTALL)", True),
    ("a call", "VALUE = sorted([2, 1])", False),
    ("a call inside a list", "VALUE = [len('a')]", False),
    ("a name", "VALUE = unittest", False),
    ("an attribute", "VALUE = unittest.TestCase", False),
    ("a binary operation", "VALUE = 'a' * 3", False),
    ("an f-string", "VALUE = f'{unittest}'", False),
    ("unpacking in a list", "VALUE = [*'ab']", False),
    ("unpacking in a dict", "VALUE = {**{}}", False),
    ("an unhashable key", "VALUE = {[1]: 2}", False),
    ("an unhashable member", "VALUE = {[1]}", False),
    ("a comprehension", "VALUE = [i for i in (1, 2)]", False),
    ("a lambda", "VALUE = lambda: 1", False),
    ("a pattern from a name", "ATOM = re.compile(unittest.__name__)", False),
    ("a pattern with a called flag", "ATOM = re.compile('a', int('2'))", False),
    ("another function of re", "ATOM = re.sub('a', 'b', 'c')", False),
    ("an attribute target", "unittest.TestCase.maxDiff = None", False),
    ("a subscript target", "re.__dict__['x'] = 1", False),
    ("a tuple target", "A, B = 1, 2", False),
    ("an annotated constant", "LIMIT: int = 3", False),
    ("an augmented assignment", "LIMIT = 1\nLIMIT += 1", False),
    ("a name pytest reads", "pytest_plugins = ['tests.helpers']", False),
    ("pytestmark", "pytestmark = 'slow'", False),
    ("a dunder", "__test__ = False", False),
    ("a hook name", "setup_module = None", False),
    ("collect_ignore", "collect_ignore = ['x.py']", False),
    ("load_tests", "load_tests = None", False),
):
    expect(f"E: new test file with {label}", pydiff.harmless_new_test_module(NEW_FILE.format(constant)), want)
    # the stores rule still flags a store outside the module's own names, and a `compile` call that is not the exempted form
    flagged = label in ("an attribute target", "a subscript target", "a pattern from a name", "a pattern with a called flag")
    expect(f"E: new test file with {label}: stores rule", pydiff.new_global_effects("", NEW_FILE.format(constant)), flagged)
# `re` must be the standard module, bound by `import re` and nothing else
for label, text, want in (
    ("re imported from elsewhere", "from tests import helpers as re\n\nATOM = re.compile('a')\n", False),
    ("re rebound", "import re\n\nATOM = re.compile('a')\nre = None\n", False),
    ("re as a parameter", "import re\n\nATOM = re.compile('a')\n\n\ndef helper(re):\n    return re\n", False),
    ("re under another name", "import re as regex\n\nATOM = regex.compile('a')\n", False),
    ("no import of re", "ATOM = re.compile('a')\n", False),
    ("plain", "import re\n\nATOM = re.compile('a')\n", True),
):
    expect(f"E: new test file, {label}", pydiff.harmless_new_test_module(text), want)
    expect(f"E: new test file, {label}: stores rule", pydiff.new_global_effects("", text), not want)
# inside a function the stores rule is as strict as before
expect("E: re.compile inside a new test", effects(TESTS + METHOD.format("        import re\n        x = re.compile('a')")), True)

# calls and tricks that change shared state without a store statement
for label, body in (
    ("clear on a class table", "import sqlglot\nsqlglot.Parser.FUNCTIONS.clear()"),
    ("environ update", "import os\nos.environ.update({})"),
    ("mock patch started", "from unittest import mock\nmock.patch.object(object, 'x').start()"),
    ("exec", "exec('x = 1')"),
    ("reload", "import importlib, sqlglot\nimportlib.reload(sqlglot)"),
    ("self rebound to a class", "import sqlglot\nself = sqlglot.Parser\nself.FUNCTIONS = {}"),
    ("nested def taking self", "import sqlglot\ndef f(self):\n    self.F = {}\nf(sqlglot.Parser)"),
    ("dict of a class", "import sqlglot\nsqlglot.Parser.__dict__"),
):
    text = METHOD.format("\n".join("        " + row for row in body.split("\n")))
    expect("E: " + label, added(text), False)
    expect("E: " + label + ", as new_global_effects", effects(TESTS + text), True)
expect("E: a helper whose parameter is called self", added("\n\ndef helper2(self):\n    self.F = 1\n"), False)
expect("E: the same store in a method of the class", added(METHOD.format("        self.F = 1")), True)
expect("E: a plain assertion", added(METHOD.format("        self.assertEqual(helper(1).real, 1)")), True)
expect("E: a store to self in a new class", added("\n\nclass B:\n    def setUp(self):\n        self.x = 1\n"), True)
expect("E: a class whose setUp clears a table", added(
    "\n\nclass B:\n    def setUp(self):\n        import sqlglot\n        sqlglot.Parser.FUNCTIONS.clear()\n"), False)

# ---- K: an ordinary new test costs nothing; one that changes shared state selects everything ----
#
# The rule looks for stores and for calls on a list of names, nothing more. An allowlist of
# calls (anything not on it selects everything) was tried on a branch and not taken: it turned
# down a quarter of the suite's own tests. These checks hold both ends in place.

K_OLD = textwrap.dedent('''\
    import unittest
    from unittest import mock
    from sqlglot import exp, parse_one
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokens import Tokenizer


    def helper(x):
        return x


    class TestA(unittest.TestCase):
        def test_one(self):
            self.assertEqual(helper(1), 1)
''')


def k_new(body, text=K_OLD):
    """(accepted as only new tests?, stores outside its locals?) for one new test method with this body."""
    new = text + METHOD.format("\n".join("        " + row for row in body.split("\n")))
    return pydiff.only_new_definitions(text, new, {"TestA::test_one"}), pydiff.new_global_effects(text, new)


def k_file(text):
    return pydiff.harmless_new_test_module(text), pydiff.new_global_effects("", text)


for label, body in (
    ("assertion", "self.assertEqual(helper(1), 1)"),
    ("module function and a local", "x = helper(2)\nself.assertTrue(x)"),
    ("method on a result", "self.assertEqual(str(helper(1)).lower(), '1')"),
    ("builtins", "self.assertEqual(len(sorted([2, 1])), 2)"),
    ("assertRaises", "with self.assertRaises(ValueError):\n    helper(1)"),
    ("subTest in a loop", "for sql in ('a', 'b'):\n    with self.subTest(sql):\n        self.assertEqual(parse_one(sql).sql(), sql)"),
    ("validate_identity and validate_all", "self.validate_identity('SELECT 1')\nself.validate_all('SELECT 1', write={'duckdb': 'SELECT 1'})"),
    ("a class method of sqlglot", "self.assertEqual(exp.Literal.number(1).sql(), '1')"),
    ("a method of a new instance", "tokens = Tokenizer().tokenize('SELECT 1')\nself.assertEqual(len(tokens), 2)"),
    ("mock.patch.object as a context manager", "with mock.patch.object(Dialect, 'get', return_value=None):\n    self.assertIsNone(Dialect.get('x'))"),
    ("find on a parsed tree", "self.assertIsNotNone(parse_one('select 1').find(exp.Select))"),
    ("locals and a loop", "total = 0\nfor i in range(3):\n    total += i\nself.assertEqual(total, 3)"),
    ("transform with a lambda", "tree = parse_one('select a').transform(lambda node: node)\nself.assertEqual(tree.sql(), 'SELECT a')"),
    ("assertLogs", "with self.assertLogs('sqlglot') as logs:\n    helper(1)\nself.assertTrue(logs)"),
    ("try and finally with no store", "handle = helper(1)\ntry:\n    self.assertTrue(handle)\nfinally:\n    handle.close()"),
):
    expect("K: an ordinary new test, " + label, k_new(body), (True, False))
for label, body in (
    ("logging.disable", "import logging\n\nlogging.disable(logging.CRITICAL)"),
    ("a store into os.environ", "import os\n\nos.environ['SQLGLOT_X'] = '1'"),
    ("a logger's level", "import sqlglot.generator as g\n\ng.logger.setLevel(60)"),
    ("setattr on a class", "setattr(Dialect, 'LIMIT_FETCH', 'FETCH')"),
    ("a class table cleared", "Dialect.TRANSFORMS.clear()"),
    ("a store through the patched object", "with mock.patch.object(Dialect, 'get') as get:\n    get.return_value = None"),
):
    expect("K: a new test that changes shared state, " + label, k_new(body), (False, True))
K_HEAD = "import unittest\nimport logging\nfrom sqlglot import exp, parse_one\n"
expect("K: new file, plain tests", k_file(K_HEAD + "\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(exp.column('a').sql(), 'a')\n        self.assertIsNotNone(parse_one('select 1').find(exp.Select))\n"), (True, False))
expect("K: new file, a helper that a test calls", k_file(K_HEAD + "\n\ndef build(sql):\n    return parse_one(sql)\n\n\ndef test_a():\n    assert build('select 1').sql() == 'SELECT 1'\n"), (True, False))
expect("K: new file, the logging level", k_file(K_HEAD + "\n\ndef test_a():\n    logging.disable(50)\n"), (False, True))
expect("K: new file, a helper that clears a class table", k_file(K_HEAD + "\n\ndef reset():\n    exp.Expr.arg_types.clear()\n\n\ndef test_a():\n    reset()\n"), (False, True))

# ---- L: an edited test that leaves state behind --------------------------------

L_OLD = textwrap.dedent('''\
    import unittest
    from sqlglot import Parser

    ORIGINAL = Parser.FUNCTIONS


    class TestA(unittest.TestCase):
        def setUp(self):
            self.n = 1

        def tearDown(self):
            self.n = 0

        def test_one(self):
            d = {}
            d["a"] = 1
            self.n = 2
            try:
                Parser.FUNCTIONS = {}
                self.assertEqual(self.n, 2)
            finally:
                Parser.FUNCTIONS = ORIGINAL

        def test_two(self):
            cls = Parser
            cls.X = 1
            self.addCleanup(print, 1)

        def test_three(self):
            def restore():
                print(1)
            self.addCleanup(restore)
            self.assertEqual(1, 1)

        def test_four(self):
            handle = open("f")
            try:
                self.assertTrue(handle)
            finally:
                handle.close()

        def test_five(self):
            first, second = Parser.FUNCTIONS, {}
            first["A"] = 1
            either = second or Parser.FUNCTIONS
            either["B"] = 2


    class TestB(unittest.TestCase):
        def tearDown(self):
            print("done")

        def test_six(self):
            try:
                self.assertEqual(1, 1)
            finally:
                print("six")
''')


def edited(old, new, text=L_OLD):
    if text.count(old) != 1:
        raise SystemExit(f"FAIL the edit {old!r} must match once, matches {text.count(old)} times")
    return pydiff.edit_leaves_state(text, text.replace(old, new))


for label, old, new, want in (
    ("value of a store in finally", "Parser.FUNCTIONS = ORIGINAL", "Parser.FUNCTIONS = {}", True),
    ("removed store in finally", "Parser.FUNCTIONS = ORIGINAL", "pass", True),
    ("value of a store in a test body", "Parser.FUNCTIONS = {}", "Parser.FUNCTIONS = {1: 2}", True),
    ("a store to self", "self.n = 2", "self.n = 3", False),
    ("a store to a fresh local", 'd["a"] = 1', 'd["a"] = 2', False),
    ("a removed store to a fresh local", '        d["a"] = 1\n', "", False),
    ("an alias of a class", "cls.X = 1", "cls.X = 2", True),
    ("a removed store through an alias", "        cls.X = 1\n", "", True),
    ("an unpacked value is an alias", '        first["A"] = 1\n', "", True),
    ("the result of `or` is an alias", '        either["B"] = 2\n', "", True),
    ("an assertion", "self.assertEqual(self.n, 2)", "self.assertEqual(self.n, 3)", False),
    ("tearDown", "self.n = 0", "self.n = 5", True),
    ("addCleanup call", "self.addCleanup(print, 1)", "self.addCleanup(print, 2)", True),
    ("a removed addCleanup call", "        self.addCleanup(print, 1)\n", "", True),
    ("addCleanup callback", "print(1)\n        self.addCleanup(restore)", "print(2)\n        self.addCleanup(restore)", True),
    ("a comment", "self.n = 2", "self.n = 2  # same", False),
    ("a new store in the body", "self.assertEqual(1, 1)\n\n    def test_four", "Parser.Z = 1\n\n    def test_four", True),
    ("setUp, self only", "self.n = 1", "self.n = 4", False),
    ("a call in finally that stores nothing, changed", "handle.close()", "handle.flush()", True),
    ("a call in finally that stores nothing, removed", "handle.close()", "pass", True),
    ("a line added to a finally block", "            handle.close()\n", "            handle.close()\n            print(2)\n", True),
    ("the try block of a finally, not the finally", "self.assertTrue(handle)", "self.assertFalse(handle)", False),
    ("a tearDown that stores nothing, changed", 'print("done")', 'print("over")', True),
    ("a tearDown taken out of a class that stays", '    def tearDown(self):\n        print("done")\n\n', "", True),
):
    expect("L: " + label, edited(old, new), want)
expect("L: text that does not parse", pydiff.edit_leaves_state(L_OLD, "def (:\n"), False)
expect("L: a new helper at the end", pydiff.edit_leaves_state(L_OLD, L_OLD + "\n\ndef helper(x):\n    return x\n"), False)
# What is new as a whole is rule K's, and what is gone as a whole keeps only its stores.
NEW_TEST = "\n    def test_new(self):\n        handle = open('g')\n        try:\n            self.assertTrue(handle)\n        finally:\n            handle.close()\n"
expect("L: a new test with a finally of its own", pydiff.edit_leaves_state(L_OLD, L_OLD + NEW_TEST), False)
expect("L: and rule K accepts it", pydiff.only_new_definitions(L_OLD, L_OLD + NEW_TEST, set()), True)
NEW_CLASS = "\n\nclass TestC(unittest.TestCase):\n    def setUp(self):\n        self.handle = open('g')\n\n    def tearDown(self):\n        self.handle.close()\n\n    def test_c(self):\n        self.assertTrue(self.handle)\n"
expect("L: a new class with a tearDown of its own", pydiff.edit_leaves_state(L_OLD, L_OLD + NEW_CLASS), False)
expect("L: a tearDown added to a class that was there", edited("    def test_two(self):", "    def tearDownClass(cls):\n        pass\n\n    def test_two(self):"), True)
expect("L: a test with a finally, removed whole", edited(
    "    def test_four(self):\n        handle = open(\"f\")\n        try:\n            self.assertTrue(handle)\n        finally:\n            handle.close()\n\n", ""), False)
expect("L: a class with a tearDown, removed whole", pydiff.edit_leaves_state(L_OLD, L_OLD[:L_OLD.index("class TestB")].rstrip("\n") + "\n"), False)
expect("L: a test that stored into a class, removed whole", edited(
    "    def test_two(self):\n        cls = Parser\n        cls.X = 1\n        self.addCleanup(print, 1)\n\n", ""), True)
expect("L: a renamed test whose finally also changed", edited(
    "def test_four(self):\n        handle = open(\"f\")\n        try:\n            self.assertTrue(handle)\n        finally:\n            handle.close()",
    "def test_fourth(self):\n        handle = open(\"f\")\n        try:\n            self.assertTrue(handle)\n        finally:\n            pass"), True)
expect("L: a form feed in a line does not shift the line numbers", pydiff.edit_leaves_state(
    L_OLD.replace("import unittest\n", "import unittest\nFF = '\x0c'\n"),
    L_OLD.replace("import unittest\n", "import unittest\nFF = '\x0c'\n").replace("Parser.FUNCTIONS = ORIGINAL", "pass")), True)

# ---- M: a new definition that fails when the module is imported ---------------

M_BASE = "import unittest\nfrom unittest import mock\nfrom sqlglot.dialects.dialect import Dialect\nfrom tests.helpers import Validator, load\n\n\nclass Base(unittest.TestCase):\n    def test_a(self):\n        pass\n"


def m_added(text, base=M_BASE):
    return pydiff.only_new_definitions(base, base + text, {"Base::test_a"})


CLASS = "\n\nclass T({}):\n    def test_b(self):\n        pass\n"
for label, bases, want in (
    ("no base", "", True),
    ("object", "object", True),
    ("a class of the module", "Base", True),
    ("unittest.TestCase", "unittest.TestCase", True),
    ("a class imported from the tests", "Validator", True),
    ("a name nothing binds", "NoSuchBase", False),
    ("an imported module", "mock", False),
    ("an imported function", "load", False),
    ("a class of sqlglot, whose metaclass acts", "Dialect", False),
    ("an attribute of another module", "mock.Mock", False),
    ("two bases may have no consistent order", "unittest.TestCase, Base", False),
    ("a repeated base", "Base, Base", False),
    ("a call", "type('B', (), {})", False),
):
    expect("M: new class, " + label, m_added(CLASS.format(bases).replace("T()", "T")), want)
expect("M: a base defined further down does not exist yet", pydiff.only_new_definitions(
    M_BASE, M_BASE.replace("\n\nclass Base", "\n\nclass T(Base):\n    def test_b(self):\n        pass\n\n\nclass Base"), {"Base::test_a"}), False)
expect("M: a base of the module that is itself plain", m_added(CLASS.format("Mid"), M_BASE + "\n\nclass Mid(Base):\n    pass\n"), True)
expect("M: a base of the module with two bases", m_added(CLASS.format("Mid"), M_BASE + "\n\nclass Mid(Base, object):\n    pass\n"), False)
expect("M: a base of the module with a subclass hook", m_added(
    CLASS.format("Mid"), M_BASE + "\n\nclass Mid(Base):\n    def __init_subclass__(cls):\n        raise TypeError\n"), False)
expect("M: a base rebound after its class statement", m_added(CLASS.format("Base"), M_BASE + "\nBase = None\n"), False)
expect("M: unittest bound to something else", m_added(CLASS.format("unittest.TestCase"), M_BASE + "\nunittest = None\n"), False)
expect("M: after a star import any name may be another thing", m_added(CLASS.format("Base"), M_BASE + "\nfrom os import *\n"), False)
expect("M: a decorator on a new def", m_added("\n\n@mock.patch('x')\ndef helper(a):\n    return a\n"), False)
expect("M: a default that runs", m_added("\n\ndef helper(a=load('x')):\n    return a\n"), False)
expect("M: a keyword on a class", m_added("\n\nclass T(Base, metaclass=type):\n    pass\n"), False)
NEW_FILE = "import unittest\nfrom sqlglot import {}\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"
RESOLVES = {("unittest", None), ("sqlglot", ("exp",))}


def resolves(module, names):
    return (module, tuple(names) if names else None) in RESOLVES


expect("M: new file, imports that can succeed", pydiff.harmless_new_test_module(NEW_FILE.format("exp"), resolves), True)
expect("M: new file, an import that cannot", pydiff.harmless_new_test_module(NEW_FILE.format("nothing"), resolves), False)
expect("M: new file, nobody asked", pydiff.harmless_new_test_module(NEW_FILE.format("nothing")), True)
expect("M: new file, a base nothing binds", pydiff.harmless_new_test_module(NEW_FILE.format("exp").replace("unittest.TestCase", "Missing")), False)
expect("M: new file, a base imported from the tests", pydiff.harmless_new_test_module(
    "from tests.dialects.test_dialect import Validator\n\n\nclass T(Validator):\n    def test_a(self):\n        self.validate_identity('SELECT 1')\n"), True)

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

TIME_TESTS = covmap.tests_of_file("tests/test_time.py")
for name in ("C_setup_module", "C_setup_method"):
    expect("seed " + name + ": the tests of the file, unchecked", seed(name).unverified & TIME_TESTS, TIME_TESTS)
for hook in ("def teardown_class(cls):", "def teardown_function(f):", "def setup_class(cls):", "def teardown_method(self):", "def setUpModule():"):
    expect("C: " + hook, plan_for(comment_patch("tests/test_time.py", hook)).unverified & TIME_TESTS, TIME_TESTS)
# pytest_plugins in a test module or in a test package: its hooks reach the tests of every file
for name in ("C_pytest_plugins_self", "C_pytest_plugins_package"):
    expect("seed " + name + ": every test", seed(name).select_all, True)
expect("C: pytest_plugins names a helper", plan_for(comment_patch("tests/test_time.py", "pytest_plugins = ['tests.helpers']")).select_all, True)
plan = plan_for(comment_patch("sqlglot/helper.py", "# pytest_plugins"))
expect("C: pytest_plugins outside tests/", (plan.candidates, plan.select_all), (0, False))
for name in sorted(os.listdir(os.path.join(HERE, "kinds_seeds"))):
    if name.startswith("E_"):
        plan = seed(name[:-6])
        expect("seed " + name[:-6] + ": not 'only new tests'", any("only new tests" in note for note in plan.notes), False)
        expect("seed " + name[:-6] + ": selected", plan.select_all or plan.candidates != 0, True)

for name in sorted(os.listdir(os.path.join(HERE, "kinds_seeds"))):
    if name.startswith(("K_", "L_")):
        expect("seed " + name[:-6], seed(name[:-6]).select_all, True)
plan = seed("M_undefined_base")
expect("seed M: not 'only new tests'", any("only new tests" in note for note in plan.notes), False)
expect("seed M: the tests of the file run", plan.candidates & covmap.tests_of_file("tests/test_helper.py"), covmap.tests_of_file("tests/test_helper.py"))
expect("seed M: and nothing is selected unchecked", (plan.select_all, plan.unverified), (False, 0))

TYPING = "tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata"
for name in sorted(os.listdir(os.path.join(HERE, "kinds_seeds"))):
    if name.startswith("B_"):
        expect("seed " + name[:-6], selected(seed(name[:-6]), TYPING), True)

# seed B_typing_alias adds an import under a name nothing reads, before the import the test looks for in the
# text. The comparison of import state reports that one added name (the row below), CovMap.unobservable calls
# it unread and the 1114 importers go. The test that reads the file must stay: in what the comparison leaves
# and in the plan (main.finish_probe: rest_candidates | what the comparison leaves).
plan = seed("B_typing_alias")
ALIAS_ROW = ["module", "sqlglot.typing.duckdb", "", "_zz", "added", "sqlglot/typing/duckdb.py", "", 0, ""]
expect("seed B_typing_alias: nothing reads the name", covmap.unobservable("module", "sqlglot/typing/duckdb.py", "", "_zz", "added", ""), True)
left = covmap.state_candidates({"differences": [ALIAS_ROW], "inconclusive": ""})
expect("seed B_typing_alias: the comparison leaves the test that reads the file", left, 1 << ID[TYPING])
expect("seed B_typing_alias: the plan keeps it apart from the importers", plan.rest_candidates, 1 << ID[TYPING])
expect("seed B_typing_alias: the importers are import-time candidates", plan.import_candidates & ~plan.rest_candidates != 0, True)
expect("seed B_typing_alias: candidates after the comparison", plan.rest_candidates | left, 1 << ID[TYPING])


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


NEW_TEST_FILE = "import unittest\n{}\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n"
for line in (
    "from sqlglot import nothing",
    "import sqlglot.nothing",
    "from sqlglot.nothing import exp",
    "from tests.helpers import nothing",
    "from tests.dialects.test_dialect import Validator, Nothing",
):
    plan = plan_for(new_file_patch("tests/test_new_import.py", NEW_TEST_FILE.format(line)))
    expect("M: a new test file with an import that cannot succeed, " + line, plan.select_all, True)
for line in (
    "from sqlglot import exp, parse_one\nfrom tests.helpers import assert_logger_contains",
    "from sqlglot.dialects import BigQuery, Hive",  # handed out by the module's __getattr__
    "from sqlglot.optimizer import optimize\nfrom sqlglot.optimizer.qualify import qualify",
    "from sqlglot.expressions import Column, convert",  # bound by star imports
    "import sqlglot.expressions.core as core\nimport sqlglot.dialects",
    "from tests.dialects.test_dialect import Validator",
    "from sqlglot import optimizer",  # a submodule
    "from decimal import Decimal\nfrom unittest import mock\nimport pandas as pd",
):
    plan = plan_for(new_file_patch("tests/test_new_import.py", NEW_TEST_FILE.format(line)))
    expect("M: a new test file whose imports can succeed, " + line.split("\n")[0], (plan.select_all, plan.candidates), (False, 0))


def appended(path, addition):
    """A patch that adds text at the end of a file of the pinned tree."""
    import difflib

    with open(os.path.join(ROOT, path), encoding="utf-8", newline="") as handle:
        old = handle.read().splitlines(keepends=True)
    body = "".join(difflib.unified_diff(old, old + addition.splitlines(keepends=True), "a/" + path, "b/" + path, n=3))
    return f"diff --git a/{path} b/{path}\n" + body


both = appended("sqlglot/time.py", "\n\ndef zz_new_helper():\n    return 1\n") + new_file_patch(
    "tests/test_new_import.py", NEW_TEST_FILE.format("from sqlglot.time import zz_new_helper"))
expect("M: a name the same patch adds is not judged", plan_for(both).select_all, False)
both = new_file_patch("sqlglot/zz_new_module.py", "VALUE = 1\n") + new_file_patch(
    "tests/test_new_import.py", NEW_TEST_FILE.format("from sqlglot.zz_new_module import VALUE"))
expect("M: a module the same patch adds is not judged", any("does more than define tests" in note for note in plan_for(both).notes), False)

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

# ---- the names half of dirs.json: only the tests that ask for a name or list a directory notice a new file ----

def empty_deleted_patch(path):
    return f"diff --git a/{path} b/{path}\ndeleted file mode 100644\nindex e69de29..0000000\n"


def config_patch(path, text):
    return new_file_patch(path, text)


if covmap._dirs["names"] is not None:
    plan = plan_for(new_file_patch("sqlglot/zz_unasked.py", "raise RuntimeError\n"))
    expect("N: a new module that nobody asks for", (plan.candidates, plan.select_all), (0, False))
    for where in ("sqlglot/dialects", "sqlglot/optimizer", "tests", "tests/dialects", ""):
        plan = plan_for(new_file_patch((where + "/" if where else "") + "zz_unasked.py", "raise RuntimeError\n"))
        expect("N: a new module that nobody asks for, in '" + where + "'", (plan.candidates, plan.select_all), (0, False))
    plan = plan_for(new_file_patch("sqlglot/dialects/myqsl.py", "raise RuntimeError\n"))
    expect("N: a dialect name given as a string by one test", (0 < plan.candidates.bit_count() < 10, plan.select_all), (True, False))
    plan = plan_for(new_file_patch("sqlglot/_version.py", "raise RuntimeError\n"))
    expect("N: a name every importer of sqlglot asks for", plan.candidates.bit_count() >= 1600, True)
    plan = plan_for(new_file_patch("json.py", "raise RuntimeError\n"))
    expect("N: a module that shadows the standard one every test loads", plan.candidates, everything)
    # a package marker that vanishes is a name pytest reads for itself: the rule for those files answers first
    plan = plan_for(empty_deleted_patch("tests/dialects/__init__.py"))
    expect("N: the package tests.dialects loses its __init__.py", plan.select_all, True)
    # names the map cannot hold: what pytest's own import hook asks for, and what an existing or installed module answers to
    for path in ("tests/test_parser.so", "tests/dialects/test_dialect.cpython-313-x86_64-linux-gnu.so", "sqlglot/parser.pyd",
                 "sqlglot/helper/__init__.py", "sqlglot/tokens/extra.py", "xdist.py", "pytest_timeout.py", "duckdb.py",
                 "typing.py", "json/__init__.py", "pandas/core.py"):
        plan = plan_for(new_file_patch(path, "X = 1\n"))
        expect("N: a new entry that shadows a module: " + path, plan.select_all or plan.candidates == everything, True)
    for path in ("zz_notes.py", "zz_dir/mod.py", "sqlglot/zz_pkg_data/mod.py", "tests/zz_helper.py", "sqlglot/zz.txt", "docs/json.md"):
        plan = plan_for(new_file_patch(path, "X = 1\n"))
        expect("N: a new entry that shadows nothing: " + path, (plan.candidates, plan.select_all), (0, False))
    for path in ("tests/test_notes.txt", "tests/fixtures/test_cases.txt", "tests/dialects/test_a.txt"):
        plan = plan_for(new_file_patch(path, ">>> 1 + 1\n2\n"))
        expect("N: a text file pytest takes for doctests: " + path, plan.select_all, True)
    plan = plan_for(new_file_patch("tests/fixtures/notes.txt", "x\n"))
    expect("N: any other text file", (plan.candidates, plan.select_all), (0, False))
    for path in ("pytest.toml", ".pytest.toml", "tests/pytest.toml", "tests/dialects/pytest.ini", "tests/pyproject.toml", "tests/setup.cfg"):
        plan = plan_for(new_file_patch(path, "[pytest]\naddopts = -x\n"))
        expect("N: pytest finds its configuration by name: " + path, plan.select_all, True)

# ---- P: a binary patch to a file that is not code -------------------------------

def binary_patch(path, new=True):
    head = "new file mode 100644\n" if new else ""
    return f"diff --git a/{path} b/{path}\n{head}index 0000000..1234567\nGIT binary patch\nliteral 1\nIcmZPo000\n\n"


plan = plan_for(binary_patch("tests/fixtures/zz_logo.png"))
expect("P: a new image nobody lists", (plan.select_all, plan.candidates, plan.unverified), (False, 0, 0))
for path in ("docs/logo.png", "sqlglot.png", "posts/zz.jpg", "tests/fixtures/notes.bin"):
    plan = plan_for(binary_patch(path))
    expect("P: a new binary file that is not code: " + path, (plan.select_all, plan.candidates, plan.unverified), (False, 0, 0))
for path in ("sqlglot/zz.dat", "sqlglot/dialects/zz.png", "zz.dist-info/RECORD", "tests/zz.egg-info/PKG-INFO", "docs/zz.egg"):
    plan = plan_for(binary_patch(path))
    expect("P: a binary file in the package or of a distribution: " + path, plan.select_all, True)
# the main session's seeds, made with `git diff --binary` against the pinned tree
EXECUTOR = covmap.tests_of_file("tests/test_executor.py")
for name, want in (("P_bin_new", 0), ("P_bin_edit", 0), ("P_bin_mixed", 0), ("P_bin_gz", EXECUTOR), ("P_bin_gz_same", EXECUTOR)):
    plan = seed(name)
    expect("seed " + name, (plan.select_all, plan.candidates, plan.unverified), (False, want, want))
gone = (
    "diff --git a/posts/onboarding_images/lineage_img.png b/posts/onboarding_images/lineage_img.png\n"
    "deleted file mode 100644\nindex 1234567..0000000\nGIT binary patch\nliteral 0\nHcmV?d00001\n\n"
)
plan = plan_for(gone)
expect("P: an image deleted", (plan.select_all, plan.candidates, plan.unverified), (False, 0, 0))
# every table file of the two TPC directories is read by the class setup of the executor tests (DuckDB reads it
# itself, which no audit hook sees): the map must name every test of that class for each of them
for directory in ("tests/fixtures/optimizer/tpc-h", "tests/fixtures/optimizer/tpc-ds"):
    for file_name in sorted(os.listdir(os.path.join(ROOT, directory))):
        if file_name.endswith(".csv.gz"):
            expect("P: readers of " + directory + "/" + file_name, (covmap.opened(directory + "/" + file_name) or 0) & EXECUTOR, EXECUTOR)
opened_paths = sorted(p for p in covmap._opens if not p.endswith((".py", ".pyc", ".so")) and "__pycache__" not in p)
if opened_paths:
    path = opened_paths[0]
    plan = plan_for(binary_patch(path, new=False))
    expect("P: a binary file a test opens: those tests, unchecked", (plan.select_all, plan.candidates != 0, plan.candidates == plan.unverified), (False, True, True))
    expect("P: and exactly the tests that open it", plan.candidates, covmap.opened(path))
for path in ("sqlglot/zz.so", "sqlglot/zz.pyc", "x.pth", "sqlglot/__pycache__/a.cpython-313.pyc", "sqlglot/zz.cpython-313-x86_64-linux-gnu.so.1", "pytest.ini",
             "tests/test_notes.txt", "tests/dialects/test_a.txt"):
    expect("P: " + path + " may be code", plan_for(binary_patch(path)).select_all or plan_for(binary_patch(path)).candidates != 0, True)


# a class named like a built-in dialect, defined where a test runs: the registry files it under that name
for name in ("K_dialect_name_in_test", "K_dialect_name_in_setup", "K_dialect_name_in_helper"):
    expect("seed " + name + ": every test", seed(name).select_all, True)
DIALECT_TEST = "import unittest\nfrom sqlglot import Dialect\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        class {}(Dialect):\n            pass\n\n        self.assertTrue(True)\n"
for class_name, everything in (("MySQL", True), ("Mysql", True), ("DUCKDB", True), ("Dialect", True), ("Spark2", True), ("MyDialect", False), ("Custom", False), ("TestMySQL", False), ("MySQLish", False)):
    plan = plan_for(new_file_patch("tests/dialects/test_zz_custom.py", DIALECT_TEST.format(class_name)))
    expect("K: a new test defines class " + class_name, plan.select_all, everything)
plan = plan_for(comment_patch("tests/test_time.py", "class Snowflake: pass"))
expect("K: the name alone decides, also in an edited file", plan.select_all, True)
plan = plan_for(comment_patch("sqlglot/helper.py", "class Snowflake: pass"))
expect("K: outside tests/ the import-time rules judge a new class", plan.select_all, False)
# no class statement of the pinned tests has such a name
import ast as _ast  # noqa: E402
taken = []
for directory, _, names in os.walk(os.path.join(ROOT, "tests")):
    for file_name in names:
        if file_name.endswith(".py"):
            with open(os.path.join(directory, file_name), "rb") as handle:
                tree = _ast.parse(handle.read())
            taken += [node.name for node in _ast.walk(tree) if isinstance(node, _ast.ClassDef) and node.name.upper() in analyze._registry_names(ROOT)]
expect("K: no class of the pinned tests is named like a built-in dialect", taken, [])


# ---- a copied file (`copy from` / `copy to`): the source stays, the copy is a new file ----

def copy_patch(source, target):
    return f"diff --git a/{source} b/{target}\nsimilarity index 100%\ncopy from {source}\ncopy to {target}\n"


if covmap._dirs["names"] is not None:
    plan = plan_for(copy_patch("sqlglot/time.py", "sqlglot/time_copy.py"))
    expect("copy: a module copied under a name nobody asks for", (plan.candidates, plan.select_all), (0, False))
    plan = plan_for(copy_patch("README.md", "docs/README_copy.md"))
    expect("copy: a text file copied", (plan.candidates, plan.select_all), (0, False))
    plan = plan_for(copy_patch("sqlglot/time.py", "json.py"))
    expect("copy: the copy still counts as a new file under its own name", plan.candidates, everything)
    plan = plan_for(copy_patch("tests/dialects/test_dune.py", "tests/dialects/test_dune_copy.py"))
    expect("copy: a plain test file copied is a harmless new test file", (plan.candidates, plan.select_all), (0, False))
    plan = plan_for(copy_patch("tests/test_dialect_entry_points.py", "tests/test_entry_points_copy.py"))
    expect("copy: a test file that stores outside its locals, copied, selects everything", plan.select_all, True)
plan = plan_for(copy_patch("sqlglot/time.py", "sqlglot/time_copy.py"))
expect("copy: the source is not taken for removed", any("removed or renamed" in note for note in plan.notes), False)

# seed add_trait_shadow gives the trait Predicate two attributes its ancestor Expr holds (is_var_len_args,
# var_len_arg_key). sqlglot's expressions are built from traits: `class RegexpLike(Expression, Binary,
# Predicate, Func)` finds both on Func, which is no ancestor of Predicate, and the added ones come first in
# that MRO. Under pytest the tests below fail with the patch (they parse REGEXP_LIKE, RLIKE or CONTAINS as
# functions and get a list where an expression was); with the readers of the ancestors only, one of them was
# a candidate. The rows are what the comparison of import state reports for the two names.
from selector import state  # noqa: E402

state.load_order(os.path.join(MAPS, "order.json"))
state.load_pinned(ROOT)
SHADOW_ROWS = [
    ["class", "sqlglot.expressions.core", "Predicate", name, "added", "sqlglot/expressions/core.py", "", 0, ""]
    for name in ("is_var_len_args", "var_len_arg_key")
]
left = covmap.state_candidates({"differences": SHADOW_ROWS, "inconclusive": ""})
for test_id in (
    "tests/dialects/test_bigquery.py::TestBigQuery::test_bigquery",
    "tests/dialects/test_dremio.py::TestDremio::test_regexp_like",
    "tests/dialects/test_exasol.py::TestExasol::test_regexp_like",
    "tests/dialects/test_presto.py::TestPresto::test_regex",
    "tests/dialects/test_spark.py::TestSpark::test_spark",
    "tests/test_optimizer.py::TestOptimizer::test_annotate_funcs",
):
    expect("seed add_trait_shadow: " + test_id.split("::", 1)[1], bool(left >> ID[test_id] & 1), True)
expect("seed add_trait_shadow: narrower than every test", left != covmap.all, True)
func_readers = covmap.attribute_readers("sqlglot.expressions.core", "Func", "is_var_len_args")
expect("seed add_trait_shadow: the map knows who reads the trait's attribute", func_readers not in (None, 0), True)
shadowed = covmap.moved_readers("sqlglot.expressions.core", "Predicate", "is_var_len_args", "added")
expect("seed add_trait_shadow: the readers of the trait's attribute are among the readers", shadowed & func_readers, func_readers)
flag = covmap.moved_readers("sqlglot.dialects.duckdb", "DuckDB", "NORMALIZE_FUNCTIONS", "added")
base = covmap.attribute_readers("sqlglot.dialects.dialect", "Dialect", "NORMALIZE_FUNCTIONS")
expect("an inherited dialect flag set in a dialect: never fewer than the readers of the ancestor", base is not None and flag & base, base)
state.PINNED = None

print(f"{checks} checks passed")
