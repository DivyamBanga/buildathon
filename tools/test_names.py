"""Unit checks for the names map (tools/build_names.py) and the rule that uses it (CovMap.unobservable).

    python tools/test_names.py

A small package is written to a temporary directory, the map is built from it, and
each clause of the rule is exercised: what makes an added or removed name visible
(read as an attribute, as a string, as a global, through a star import, a dunder,
a `_sql` name, a class with a base from outside, a registering class) and what
leaves it unobservable.
"""
import json
import os
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import build_names  # noqa: E402
from selector.covmap import CovMap  # noqa: E402

FILES = {
    "sqlglot/alpha.py": """
        import os
        from sqlglot.beta import *
        from sqlglot.gamma import *

        USED_GLOBAL = 1
        UNREAD = 2
        ONLY_STORED = 3

        class Thing:
            KNOWN = 1
            ORDER = (KNOWN,)

            def run(self):
                self.touched = max(1, 2)
                return len(USED_GLOBAL_ALIAS)

        def helper():
            return USED_GLOBAL + BETA_NAME + GAMMA_NAME + DELTA_NAME + os.sep

        USED_GLOBAL_ALIAS = [1]
    """,
    "sqlglot/beta.py": """
        BETA_NAME = 1
        from_beta_unread = 1
    """,
    "sqlglot/gamma.py": """
        from sqlglot.delta import *

        GAMMA_NAME = 2
    """,
    "sqlglot/delta.py": """
        DELTA_NAME = 1
    """,
    "sqlglot/user.py": """
        from sqlglot.alpha import Thing

        def use(obj, name):
            obj.attr_read
            obj.attr_written = 1
            getattr(obj, "by_string")
            return {"dotted.part": 1, "a, b, c": 2, "m:entry": 3, "one two three four five six seven eight nine": 4, "key": 5}

        def delta_user():
            return DELTA_NAME
    """,
    "tests/test_x.py": """
        import sqlglot.alpha as alpha

        class FromTest:
            pass

        def test_it():
            alpha.read_by_test
    """,
}
CLASSES = {
    "sqlglot/alpha.py": {"Thing": "other", "Mode": "enum"},
    "sqlglot/user.py": {"Local": "other"},
}


def make_map():
    root = tempfile.mkdtemp()
    for relative, source in FILES.items():
        path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(textwrap.dedent(source))
    maps = tempfile.mkdtemp()
    build_names.main(root, maps)
    for name, content in (("tests.json", []), ("cov.json", {}), ("opens.json", {}), ("classes.json", CLASSES)):
        with open(os.path.join(maps, name), "w") as handle:
            json.dump(content, handle)
    covmap = CovMap(maps)
    covmap._pack = lambda name: {}  # the class kinds need these packs to exist
    return covmap, json.load(open(os.path.join(maps, "names.json")))


covmap, data = make_map()
checks = 0


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        print(f"FAIL {label}: got {got}, want {want}")
        sys.exit(1)


def module(path, name, reason="added", shape="", file="sqlglot/alpha.py"):
    return covmap.unobservable("module", file, "", name, reason, shape)


def klass(name, reason="added", shape="", qualname="Thing", file="sqlglot/alpha.py"):
    return covmap.unobservable("class", file, qualname, name, reason, shape)


names = set(data["named"])
expect("attribute loads are named", {"attr_read", "attr_written", "read_by_test", "touched"} <= names, True)
expect("a defined name is not named", "UNREAD" in names or "ONLY_STORED" in names, False)
expect("string constants are named", {"by_string", "key", "dotted", "part", "a", "b", "c", "m", "entry"} <= names, True)
expect("a sentence of more than eight words is not split", "three" in names or "nine" in names, False)
expect("builtins read by a file are its globals", "max" in data["globals"]["sqlglot/alpha.py"], True)
expect("classes are listed", {"Thing"} <= set(data["classes"]), True)
expect("stars are resolved", data["stars"]["sqlglot/alpha.py"], ["sqlglot/beta.py", "sqlglot/gamma.py"])

# modules
expect("an unread constant added to a module", module("m", "ZZ_ADDED"), True)
expect("an unread constant removed from a module", module("m", "UNREAD", "removed"), True)
expect("a name never read but defined elsewhere", module("m", "from_beta_unread", file="sqlglot/beta.py"), True)
expect("read as an attribute elsewhere", module("m", "attr_read"), False)
expect("stored as an attribute elsewhere", module("m", "attr_written"), False)
expect("read by a test", module("m", "read_by_test"), False)
expect("held as a string", module("m", "by_string"), False)
expect("part of a dotted string", module("m", "part"), False)
expect("a global the module's own code loads", module("m", "USED_GLOBAL_ALIAS"), False)
expect("a builtin the module calls (shadowing it)", module("m", "max"), False)
expect("a builtin the module does not call", module("m", "zip"), True)
expect("a global loaded through a star import (one step)", module("m", "BETA_NAME", file="sqlglot/beta.py"), False)
expect("a global loaded through a star import chain", module("m", "DELTA_NAME", file="sqlglot/delta.py"), False)
expect("a name only the star importer defines", module("m", "from_beta_unread", file="sqlglot/delta.py"), True)
expect("a dunder", module("m", "__zz__"), False)
expect("a dunder with an unread name", module("m", "__reduce__"), False)
expect("a *_sql name whose stem is the key of a class", module("m", "thing_sql"), False)
expect("a *_sql name whose stem is a name something reads", module("m", "attr_read_sql"), False)
expect("a *_sql name whose stem is no key", module("m", "zzadded_sql"), True)
expect("a changed value is no business of the rule", module("m", "ZZ_ADDED", "value"), False)
expect("a whole-module removal", module("m", "*", "removed"), False)
expect("a file the map does not know", module("m", "ZZ_ADDED", file="sqlglot/new.py"), False)
expect("a file outside the package", module("m", "ZZ_ADDED", file="tests/test_x.py"), False)
expect("a registering class or a foreign module", module("m", "ZZ_ADDED", shape="R"), False)
expect("a row with no shape", module("m", "ZZ_ADDED", shape=None), False)

# classes
expect("an unread constant added to a class", klass("ZZ_FLAG"), True)
expect("an unread method removed from a class", klass("zz_method", "removed"), True)
expect("read as an attribute", klass("attr_read"), False)
expect("read by a test as an attribute", klass("read_by_test"), False)
expect("read in the class body or the file as a global", klass("USED_GLOBAL_ALIAS"), False)
expect("a builtin the file calls", klass("max"), False)
expect("an owner with a base from outside", klass("ZZ_FLAG", shape="F"), False)
expect("a registering class value", klass("ZzClass", shape="R"), False)
expect("a dunder on a class", klass("__len__"), False)
expect("a _sql method whose stem is a key", klass("thing_sql"), False)
expect("a _sql method whose stem is a key, in any case", klass("Thing_sql"), False)
expect("a _sql method whose stem is no key", klass("zzadded_sql"), True)
expect("the classes of the tests count as keys", klass("fromtest_sql"), False)
expect("an enum owner", klass("ZZ_FLAG", qualname="Mode"), False)
expect("an unknown class", klass("ZZ_FLAG", qualname="Nope"), False)
expect("a class defined in a function", klass("ZZ_FLAG", qualname="f.<locals>.C"), False)
expect("class owner outside the package", klass("ZZ_FLAG", file="tests/test_x.py"), False)

# A file some test reads as text (maps/sources.json): a name nothing reads is still seen by that test.
covmap._sources = {"sqlglot/alpha.py": [3]}
covmap._index = {"sqlglot/alpha.py": [0, 0], "sqlglot/beta.py": [0, 0]}
covmap._tindex = {"sqlglot/alpha.py": [0, 0]}


def seen_by(*rows):
    return covmap.state_candidates({"differences": [list(row) for row in rows], "inconclusive": ""})


ALPHA = ("module", "sqlglot.alpha", "", "ZZ_ADDED", "added", "sqlglot/alpha.py", "", 0, "")
BETA = ("module", "sqlglot.beta", "", "ZZ_ADDED", "added", "sqlglot/beta.py", "", 0, "")
THING = ("class", "sqlglot.alpha", "Thing", "ZZ_FLAG", "added", "sqlglot/alpha.py", "", 0, "")
expect("the rule still calls the name unread", module("m", "ZZ_ADDED"), True)
expect("an unread name in a file read as text: its readers", seen_by(ALPHA), 0b1000)
expect("an unread name removed from it: its readers", seen_by(ALPHA[:3] + ("UNREAD", "removed") + ALPHA[5:]), 0b1000)
expect("an unread class attribute in it: its readers", seen_by(THING), 0b1000)
expect("an unread name in a file nobody reads as text: nobody", seen_by(BETA), 0)
expect("both: the readers of the one", seen_by(BETA, ALPHA), 0b1000)

# who reads a class attribute that is removed, or added under the name of an inherited one
import types  # noqa: E402

from selector import state  # noqa: E402

fake = types.ModuleType("pkg.m")


class Base:
    FLAG = 1
    OTHER = 2
    mid = 3


class Child(Base):
    pass


class Grand(Child):
    FLAG = 4


class Leaf(Grand):
    pass


for klass_ in (Base, Child, Grand, Leaf):
    klass_.__module__ = "pkg.m"
    klass_.__qualname__ = klass_.__name__
    setattr(fake, klass_.__name__, klass_)
state.PINNED = ("/root", [], {"pkg.m": fake}, {}, {}, 0)
bitmaps = {("pkg.m", "Base", "FLAG"): 0b0110, ("pkg.m", "Base", "OTHER"): 0b1000, ("pkg.m", "Grand", "FLAG"): 0b10000}
covmap.attribute_readers = lambda module, qualname, attribute: bitmaps.get((module, qualname, attribute))
expect("removed: the readers of the pair", covmap.moved_readers("pkg.m", "Base", "FLAG", "removed"), 0b0110)
expect("removed: a pair the map does not cover", covmap.moved_readers("pkg.m", "Base", "mid", "removed"), None)
expect("added over an inherited name: the readers of the ancestor", covmap.moved_readers("pkg.m", "Child", "OTHER", "added"), 0b1000)
expect("added over a name two ancestors define: both", covmap.moved_readers("pkg.m", "Leaf", "FLAG", "added"), 0b10110)
expect("added over a name one ancestor defines", covmap.moved_readers("pkg.m", "Child", "FLAG", "added"), 0b0110)
expect("added over a name an ancestor defines and the map does not cover", covmap.moved_readers("pkg.m", "Child", "mid", "added"), None)
expect("added under a name no ancestor defines", covmap.moved_readers("pkg.m", "Child", "NEW_ONE", "added"), None)
expect("added to a class the pinned world does not have", covmap.moved_readers("pkg.m", "Nope", "FLAG", "added"), None)
expect("an uncovered ancestor next to a covered one", covmap.moved_readers("pkg.m", "Leaf", "mid", "added"), None)
state.PINNED = None
expect("without the pinned world nothing is answered", covmap.moved_readers("pkg.m", "Base", "FLAG", "removed"), None)

# without the map nothing is unobservable
empty = CovMap(covmap.directory)
empty._names = (frozenset(), {}, {}, frozenset())
expect("no map", empty.unobservable("module", "sqlglot/alpha.py", "", "ZZ_ADDED", "added", ""), False)
print(f"{checks} checks passed")
