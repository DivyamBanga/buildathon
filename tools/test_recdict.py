"""Checks of tools/recdict.py: every way of reading a dict is noted as one key or as the whole table.

    python tools/test_recdict.py
"""

import collections
import copy
import functools
import json
import operator
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recdict  # noqa: E402
from recdict import RecDict  # noqa: E402

failed = 0


def _catch(d):
    try:
        d["nope"]
    except KeyError as error:
        return error


def _update(d):
    target = {}
    target.update(d)
    return target


def _unpack(d):
    a, b, c = d
    return a, b, c




def fresh():
    recdict.KEYS.clear()
    recdict.WHOLE.clear()
    return RecDict({"a": 1, "b": 2, "c": 3}, ("m:C|T",))


def noted(table):
    recdict.WINDOW_KEYS = recdict.WINDOW_WHOLE = None
    return {key for t, key in recdict.KEYS if t == id(table)}, id(table) in recdict.WHOLE


def check(name, action, keys=frozenset(), whole=False, same=None):
    global failed
    table = fresh()
    plain = {"a": 1, "b": 2, "c": 3}
    result = action(table)
    reference = action(plain)
    got_keys, got_whole = noted(table)
    ok = got_keys == set(keys) and got_whole == whole
    if same is not False and not (result == reference or repr(result) == repr(reference)):
        ok = False
        print("   result differs:", result, reference)
    print(("ok   " if ok else "FAIL ") + name, "" if ok else f"keys {got_keys} whole {got_whole}")
    if not ok:
        failed += 1


# one key
check("d[k]", lambda d: d["a"], {"a"})
check("d.get(k)", lambda d: d.get("zz"), {"zz"})
check("k in d", lambda d: "b" in d, {"b"})
check("k not in d", lambda d: "q" not in d, {"q"})
check("d.pop(k, default)", lambda d: d.pop("a", None), {"a"})
check("d.setdefault", lambda d: d.setdefault("a", 9), {"a"})
check("operator.getitem", lambda d: operator.getitem(d, "c"), {"c"})
check("KeyError is the same", lambda d: [type(e).__name__ for e in [_catch(d)]], {"nope"})
check("str.format_map", lambda d: "{a}-{b}".format_map(d), {"a", "b"})
check("'%(a)s' % d", lambda d: "%(a)s" % d, {"a"})

# the whole table
check("iteration", lambda d: list(d), whole=True)
check("for", lambda d: [k for k in d], whole=True)
check("sorted", lambda d: sorted(d), whole=True)
check("len", lambda d: len(d), whole=True)
check("bool", lambda d: bool(d), whole=True)
check("keys", lambda d: list(d.keys()), whole=True)
check("values", lambda d: list(d.values()), whole=True)
check("items", lambda d: list(d.items()), whole=True)
check("reversed", lambda d: list(reversed(d)), whole=True)
check("popitem", lambda d: d.popitem(), whole=True)
check("copy()", lambda d: dict(d.copy()), whole=True)
check("dict(d)", lambda d: dict(d), {"a", "b", "c"}, whole=True)
check("{**d}", lambda d: {**d}, {"a", "b", "c"}, whole=True)
check("dict.update(d)", lambda d: {"z": 0} | {} if False else _update(d), {"a", "b", "c"}, whole=True)
check("f(**d)", lambda d: (lambda **kw: sorted(kw))(**d), {"a", "b", "c"}, whole=True)
check("d | other", lambda d: d | {"x": 1}, {"a", "b", "c"}, whole=True)
check("other | d", lambda d: {"x": 1} | d, {"a", "b", "c"}, whole=True)
check("d == other", lambda d: d == {"a": 1, "b": 2, "c": 3}, whole=True)
check("other == d", lambda d: {"a": 1, "b": 2, "c": 3} == d, whole=True)
check("d != other", lambda d: d != {}, whole=True)
check("repr", lambda d: repr(d), whole=True)
check("str", lambda d: str(d), whole=True)
check("format", lambda d: f"{d}", whole=True)
check("json.dumps", lambda d: json.dumps(d), whole=True)
check("json.dumps sort_keys", lambda d: json.dumps(d, sort_keys=True), whole=True)
check("pickle", lambda d: pickle.loads(pickle.dumps(d)), whole=True)
check("pickle protocol 0", lambda d: pickle.loads(pickle.dumps(d, 0)), whole=True)
check("copy.copy", lambda d: dict(copy.copy(d)), whole=True)
check("copy.deepcopy", lambda d: dict(copy.deepcopy(d)), whole=True)
check("list(d.items()) in a comprehension", lambda d: {k: v for k, v in d.items()}, whole=True)
check("dict.fromkeys(d)", lambda d: dict.fromkeys(d), whole=True)
check("collections.ChainMap", lambda d: list(collections.ChainMap(d)), whole=True)
check("ChainMap lookup", lambda d: collections.ChainMap(d)["a"], {"a"})
check("zip(d, ...)", lambda d: list(zip(d, d)), whole=True)
check("max(d)", lambda d: max(d), whole=True)
check("set(d)", lambda d: set(d), whole=True)
check("tuple(d)", lambda d: tuple(d), whole=True)
check("any(d)", lambda d: any(d), whole=True)
check("functools.reduce", lambda d: functools.reduce(lambda x, y: x + y, d), whole=True)
check("enumerate", lambda d: list(enumerate(d)), whole=True)
check("unpack a, b, c = d", lambda d: tuple(_unpack(d)), whole=True)
check("star in a list", lambda d: [*d], whole=True)
check("for with unpack", lambda d: [k for k, v in d.items()], whole=True)
check("isinstance dict", lambda d: isinstance(d, dict), same=True)
check("sum(d.values())", lambda d: sum(d.values()), whole=True)
check("class creation from namespace", lambda d: sorted(vars(type("X", (), d))) if "a" else None, {"a", "b", "c"}, whole=True, same=False)

# what a subclass cannot hide: the cases tools/trace_keys.py has to leave "whole" or avoid
print("type(d) is dict ->", type(fresh()) is dict, "(the reason a table that code tests this way cannot be wrapped)")


# mutation is not a read
table = fresh()
table["x"] = 1
del table["x"]
table.update({"y": 1})
table.clear()
print(("ok   " if noted(table) == (set(), False) else "FAIL ") + "writes are not reads")
if noted(table) != (set(), False):
    failed += 1


# ---- wrap: which tables of watched descriptors get a RecDict ------------------


class _Watch:
    __slots__ = ("value", "key")

    def __init__(self, value, key):
        self.value = value
        self.key = key

    def __get__(self, instance, owner=None):
        return self.value


class _Loose:  # a descriptor without slots: its __dict__ is the holder
    def __init__(self, value):
        self.value = value

    def __get__(self, instance, owner=None):
        return self.value


def _expect(name, ok):
    global failed
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        failed += 1


def _default(table={"k": 1}):
    return table


class _A:
    ALONE = {"x": 1, "y": 2}
    SHARED = {"s": 1}
    LOOSE = {"l": 1}
    DEFAULT = _default.__defaults__[0]
    NUMBER = 3


class _B(_A):
    SHARED = _A.SHARED


_held_by_global = {"g": 1}


class _C:
    GLOBAL = _held_by_global
    INSTANCE = {"i": 1}


class _Keeper:
    def __init__(self):
        self.kept = _C.INSTANCE


_keeper = _Keeper()
_watched = {}
for _cls, _name, _kind in (
    (_A, "ALONE", _Watch), (_A, "SHARED", _Watch), (_B, "SHARED", _Watch), (_A, "LOOSE", _Loose),
    (_A, "DEFAULT", _Watch), (_A, "NUMBER", _Watch), (_C, "GLOBAL", _Watch), (_C, "INSTANCE", _Watch),
):
    _value = vars(_cls)[_name]
    _descriptor = _kind(_value, f"m:{_cls.__name__}|{_name}") if _kind is _Watch else _kind(_value)
    setattr(_cls, _name, _descriptor)
    _watched[f"m:{_cls.__name__}|{_name}"] = _descriptor
del _value, _descriptor
recdict.KEYS.clear()
recdict.WHOLE.clear()
_keys, _whole = recdict.wrap(_watched)
_expect("wrap: tables only descriptors hold get a RecDict", _keys == ["m:_A|ALONE", "m:_A|LOOSE", "m:_A|SHARED", "m:_B|SHARED"])
_expect("wrap: a table also held by a default, a module global or an instance stays whole",
        sorted(_whole) == ["m:_A|DEFAULT", "m:_C|GLOBAL", "m:_C|INSTANCE"]
        and "tuple" in _whole["m:_A|DEFAULT"] and "dict" in _whole["m:_C|GLOBAL"]
        and ("_Keeper" in _whole["m:_C|INSTANCE"] or "dict" in _whole["m:_C|INSTANCE"]))  # 3.13 keeps instance values inline
_expect("wrap: one RecDict for every pair of the same object", _A.SHARED is _B.SHARED and type(_A.SHARED) is RecDict
        and _A.SHARED._br_pairs == ("m:_A|SHARED", "m:_B|SHARED"))
_expect("wrap: other values are left as they were", _A.NUMBER == 3 and type(_A.DEFAULT) is dict and type(_C.GLOBAL) is dict)
_A.ALONE["x"], _A().ALONE.get("nope"), _B.SHARED["s"], _A.LOOSE["l"]
_notes = {(recdict.TABLES[t]._br_pairs, key) for t, key in recdict.KEYS}
_expect("wrap: lookups through class, subclass and instance are key notes with the pairs",
        _notes == {(("m:_A|ALONE",), "x"), (("m:_A|ALONE",), "nope"), (("m:_A|SHARED", "m:_B|SHARED"), "s"), (("m:_A|LOOSE",), "l")}
        and not recdict.WHOLE)
len(_A.ALONE)
_expect("wrap: a whole read names the table", {recdict.TABLES[t]._br_pairs for t in recdict.WHOLE} == {("m:_A|ALONE",)})

print("failed:", failed)
sys.exit(1 if failed else 0)
