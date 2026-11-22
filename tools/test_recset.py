"""Checks of tools/recset.py: every way of reading a set is noted as one member or as the whole set.

    python tools/test_recset.py
"""

import collections.abc
import copy
import functools
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recdict  # noqa: E402
import recset  # noqa: E402
from recset import RecSet  # noqa: E402

failed = 0
BASE = ("a", "b", "c")


def run(name, action, keys=frozenset(), whole=False, frozen=True):
    global failed
    recdict.KEYS.clear()
    recdict.WHOLE.clear()
    kind = frozenset if frozen else set
    table = RecSet(kind(BASE), ("m:C|T",))
    plain = kind(BASE)
    try:
        got = action(table)
    except Exception as error:  # noqa: BLE001
        got = type(error)
    try:
        want = action(plain)
    except Exception as error:  # noqa: BLE001
        want = type(error)
    if isinstance(got, RecSet):
        got = got._data
    notes = {key for t, key in recdict.KEYS if t == id(table)}
    is_whole = id(table) in recdict.WHOLE
    same = got == want or (isinstance(want, (str, bytes)) is False and type(got) is type(want) and str(got) == str(want))
    ok = notes == set(keys) and is_whole == whole and same
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  notes {notes} whole {is_whole} got {got!r} want {want!r}"))
    if not ok:
        failed += 1


run("in", lambda s: "a" in s, keys={"a"})
run("not in, a miss", lambda s: "zz" in s, keys={"zz"})
run("iterate", lambda s: sorted(s), whole=True)
run("for loop", lambda s: [x for x in s].sort() or 1, whole=True)
run("len", lambda s: len(s), whole=True)
run("bool", lambda s: bool(s), whole=True)
run("repr", lambda s: repr(s), whole=True)
run("equality", lambda s: s == frozenset(BASE), whole=True)
run("inequality", lambda s: s != frozenset(BASE), whole=True)
run("subset <=", lambda s: s <= frozenset(BASE + ("d",)), whole=True)
run("superset >", lambda s: s > frozenset(("a",)), whole=True)
run("or", lambda s: s | {"x"}, whole=True)
run("or on the right, plain frozenset left", lambda s: frozenset(("x",)) | s, whole=True)
run("or on the right, plain set left", lambda s: {"x"} | s, whole=True)
run("and", lambda s: s & {"a"}, whole=True)
run("and on the right", lambda s: {"a"} & s, whole=True)
run("sub", lambda s: s - {"a"}, whole=True)
run("sub on the right", lambda s: {"a", "q"} - s, whole=True)
run("xor", lambda s: s ^ {"a", "q"}, whole=True)
run("union", lambda s: s.union({"x"}), whole=True)
run("intersection", lambda s: s.intersection({"a"}), whole=True)
run("difference", lambda s: s.difference({"a"}), whole=True)
run("symmetric_difference", lambda s: s.symmetric_difference({"a"}), whole=True)
run("issubset", lambda s: s.issubset({"a", "b", "c", "d"}), whole=True)
run("issuperset", lambda s: s.issuperset({"a"}), whole=True)
run("isdisjoint", lambda s: s.isdisjoint({"q"}), whole=True)
run("copy", lambda s: s.copy(), whole=True)
run("copy.copy", lambda s: copy.copy(s), whole=True)
run("copy.deepcopy", lambda s: copy.deepcopy(s), whole=True)
run("hash", lambda s: hash(s) == hash(frozenset(BASE)), whole=True)
run("pickle", lambda s: pickle.loads(pickle.dumps(s)), whole=True)
run("sorted", lambda s: sorted(s), whole=True)
run("list", lambda s: sorted(list(s)), whole=True)
run("tuple", lambda s: sorted(tuple(s)), whole=True)
run("set()", lambda s: set(s), whole=True)
run("frozenset()", lambda s: frozenset(s), whole=True)
run("min", lambda s: min(s), whole=True)
run("any", lambda s: any(x == "b" for x in s), whole=True)
run("join", lambda s: ",".join(sorted(s)), whole=True)
run("unpack", lambda s: sorted([*s]), whole=True)
run("a.update(s)", lambda s: set().__class__(["z"]).__or__(s) if False else (lambda t: (t.update(s), sorted(t))[1])(set()), whole=True)
run("a.issubset(s) on a plain set", lambda s: {"a"}.issubset(s), whole=True)
run("a.union(s) on a plain frozenset", lambda s: frozenset(("q",)).union(s), whole=True)
run("a == s with a a plain set", lambda s: {"a", "b", "c"} == s, whole=True)
run("a <= s with a a plain frozenset", lambda s: frozenset(("a",)) <= s, whole=True)
run("a.isdisjoint(s)", lambda s: frozenset(("q",)).isdisjoint(s), whole=True)
run("dict.fromkeys", lambda s: sorted(dict.fromkeys(s)), whole=True)
run("functools.reduce", lambda s: sorted(functools.reduce(lambda a, b: a + b, sorted(s), "")), whole=True)
run("abc.Set", lambda s: isinstance(s, collections.abc.Set), whole=False)
run("add", lambda s: s.add("q"), frozen=False)
run("discard a member", lambda s: s.discard("a"), keys={"a"}, frozen=False)
run("remove a member", lambda s: s.remove("a"), keys={"a"}, frozen=False)
run("remove a miss raises", lambda s: s.remove("zz"), keys={"zz"}, frozen=False)
run("pop", lambda s: len([s.pop()]), whole=True, frozen=False)
run("clear", lambda s: s.clear(), frozen=False)
run("update", lambda s: s.update({"q"}), frozen=False)
run("|=", lambda s: (s.__ior__({"q"}), 1)[1], frozen=False)
run("&=", lambda s: s.__iand__({"a"}) and 1, whole=True, frozen=False)
run("-=", lambda s: s.__isub__({"a"}) and 1, whole=True, frozen=False)

# naming of an equal member: the member the set holds
import enum  # noqa: E402


class Dialects(str, enum.Enum):
    HIVE = "hive"


table = RecSet(frozenset(("hive", 1)), ("m:C|T",))
for label, key, want in (("str enum finds the string", Dialects.HIVE, "hive"), ("True finds 1", True, 1)):
    got = recset.stored(table, key)
    if got != want or type(got) is not type(want):
        print("FAIL", label, got)
        failed += 1
    else:
        print("ok   stored:", label)
print("failed:", failed)
sys.exit(1 if failed else 0)
