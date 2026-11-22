"""Checks of the dispatch stand-in of tools/trace_keys.py (followed) and of recdict's MUTED.

    python tools/test_follow.py

The builder used here is the shape of sqlglot.generator._build_dispatch: a copy of cls.TRANSFORMS plus
one entry per `*_sql` method whose class is not a key of the table.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("BR_ROOT", os.path.dirname(HERE))
import recdict  # noqa: E402
from recdict import RecDict  # noqa: E402
import trace_keys  # noqa: E402

failed = 0


def check(name, condition, detail=""):
    global failed
    if not condition:
        failed += 1
        print("FAIL", name, detail)


class A: ...
class B: ...
class C: ...
class D(A): ...  # a subclass of a class that has an entry


EXPR = {"a": A, "b": B, "c": C, "d": D}
CALLS = []


def build(cls):
    """The reviewed shape: dict(cls.TRANSFORMS), then the methods of classes that are not keys."""
    CALLS.append(cls)
    dispatch = dict(cls.TRANSFORMS)
    for name in dir(cls):
        if not name.endswith("_sql") or name.startswith("_"):
            continue
        klass = EXPR.get(name[:-4])
        if klass and klass not in dispatch:
            dispatch[klass] = getattr(cls, name)
    return dispatch


def reset():
    recdict.KEYS.clear()
    recdict.WHOLE.clear()
    recdict.MUTED = None


def notes(table):
    keys = {key for table_id, key in recdict.KEYS if table_id == id(table)}
    return keys, id(table) in recdict.WHOLE


def pairs_noted():
    """{pair: (set of keys, whole)} over every table that made a note, as trace_keys.snapshot unites them."""
    found = {}
    for table_id, key in recdict.KEYS:
        for pair in recdict.TABLES[table_id]._br_pairs:
            found.setdefault(pair, [set(), False])[0].add(key)
    for table_id in recdict.WHOLE:
        for pair in recdict.TABLES[table_id]._br_pairs:
            found.setdefault(pair, [set(), False])[1] = True
    return {pair: (keys, whole) for pair, (keys, whole) in found.items()}


def base_table():
    return RecDict({A: "ta", B: "tb"}, ["m:Gen|TRANSFORMS"])


class Gen:
    TRANSFORMS = base_table()

    def a_sql(self): ...  # A is a key of the table: the entry wins
    def c_sql(self): ...  # C is not: the method is the entry
    def _hidden_sql(self): ...


class Sub(Gen):  # inherits the table without one of its own
    def b_sql(self): ...


follow = trace_keys.followed(build)

# 1. The copy makes no note; the result holds what the builder made.
reset()
dispatch = follow(Gen)
check("the copy is a RecDict", type(dispatch) is RecDict)
check("copying is not noted", notes(Gen.TRANSFORMS) == (set(), False), str(notes(Gen.TRANSFORMS)))
check(
    "same content as the builder's, in its order",
    list(dict.items(dispatch)) == [(A, "ta"), (B, "tb"), (C, Gen.c_sql)],
    str(dict(dict.items(dispatch))),
)
check("the copy made no note either", notes(dispatch) == (set(), False))
check("MUTED is released", recdict.MUTED is None)
check("the copy stands for the table's pairs", dispatch._br_pairs == ("m:Gen|TRANSFORMS",))

# 2. A lookup that hits an entry of the table, one that hits a method, one that misses: each a key note.
reset()
check("hit", dispatch.get(A) == "ta")
check("method entry", dispatch.get(C) is not None)
check("miss", dispatch.get(D) is None)
check("three key notes on the pair", pairs_noted() == {"m:Gen|TRANSFORMS": ({A, C, D}, False)}, str(pairs_noted()))

# 3. A subclass of an expression class is looked up by its own class only: no note for the base.
reset()
dispatch.get(D)
check("no note by MRO", pairs_noted() == {"m:Gen|TRANSFORMS": ({D}, False)})

# 4. Anything else done with the copy is a whole read of the table.
for label, action in (
    ("iteration", lambda d: list(d)),
    ("len", lambda d: len(d)),
    ("items", lambda d: list(d.items())),
    ("copy", lambda d: dict(d)),
    ("equality", lambda d: d == {}),
    ("bool", lambda d: bool(d)),
):
    reset()
    action(dispatch)
    check(f"{label} of the copy is a whole read", pairs_noted().get("m:Gen|TRANSFORMS", (set(), False))[1] is True, str(pairs_noted()))

# 5. A generator class that inherits the table: the notes go to the pair that holds the dict.
reset()
inherited = follow(Sub)
check("inherited: nothing noted by the copy", pairs_noted() == {})
inherited.get(B)
check("inherited: the note is on the holder's pair", pairs_noted() == {"m:Gen|TRANSFORMS": ({B}, False)}, str(pairs_noted()))
check("inherited: the entry wins over the subclass method", dict.get(inherited, B) == "tb")

# 6. Direct reads of the table stay what they were.
reset()
Gen.TRANSFORMS.get(A)
A in Gen.TRANSFORMS
check("direct key reads", notes(Gen.TRANSFORMS) == ({A}, False))
reset()
{**Gen.TRANSFORMS}
check("a direct copy is whole", notes(Gen.TRANSFORMS)[1] is True)

# 7. A table that is not a RecDict (a class made in a test, a table left whole) is not followed.
class Own(Gen):
    TRANSFORMS = {**dict.copy(Gen.TRANSFORMS), C: "tc"}


reset()
plain = follow(Own)
check("a plain table is not followed", type(plain) is dict and plain[C] == "tc")
check("and nothing is noted for the base", pairs_noted() == {})

# 8. A change of the table at test time: a later copy sees it, an earlier copy does not (as the real cache).
reset()
table = base_table()


class Gen2:
    TRANSFORMS = table


early = follow(Gen2)
table.pop(A, None)
late = follow(Gen2)
check("pop is a key note", notes(table)[0] == {A})
check("the earlier copy keeps the entry", dict.__contains__(early, A))
check("the later copy does not have it", not dict.__contains__(late, A))

# 9. A builder that raises leaves nothing muted; a builder that returns something else is a whole read.
def broken(cls):
    dict(cls.TRANSFORMS)
    raise RuntimeError("no")


reset()
try:
    trace_keys.followed(broken)(Gen)
except RuntimeError:
    pass
check("MUTED released after an error", recdict.MUTED is None)
reset()
odd = trace_keys.followed(lambda cls: list(cls.TRANSFORMS))(Gen)
check("another kind of result is a whole read", notes(Gen.TRANSFORMS)[1] is True and type(odd) is list)

# 10. Nested use (a builder that builds another class's table) is not muted twice.
def nesting(cls):
    if cls is Gen:
        follow(Gen2)
    return dict(cls.TRANSFORMS)


reset()
trace_keys.followed(nesting)(Gen)
check("the inner copy is noted, since only one table can be muted", notes(table)[1] is True, str(notes(table)))

# 11. MUTED silences exactly one table.
reset()
other = RecDict({"x": 1}, ["m:O|T"])
recdict.MUTED = id(other)
other.get("x")
list(other)
Gen.TRANSFORMS.get(B)
recdict.MUTED = None
check("the muted table made no note", notes(other) == (set(), False))
check("another table is noted meanwhile", notes(Gen.TRANSFORMS) == ({B}, False))

# 12. Equality of two recording dicts notes both sides.
reset()
left, right = RecDict({"x": 1}, ["m:L|T"]), RecDict({"x": 1}, ["m:R|T"])
check("==", left == right)
check("== notes both", notes(left)[1] and notes(right)[1])
reset()
check("!=", not (left != right))
check("!= notes both", notes(left)[1] and notes(right)[1])

# 13. The reviewed source: the digest in trace_keys is the one of the pinned function (when it can be read here).
root = os.environ.get("BR_PINNED") or os.path.join(os.path.dirname(os.path.dirname(HERE)), "sqlglot")
source = os.path.join(root, "sqlglot", "generator.py")
if os.path.exists(source):
    import hashlib

    text = open(source, encoding="utf-8", newline="").read().replace("\r\n", "\n")
    lines = text[text.index("def _build_dispatch("):text.index("class Generator:")].split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    digest = hashlib.sha1(("\n".join(lines) + "\n").encode()).hexdigest()[:16]
    check("digest of the reviewed builder", digest == trace_keys.BUILD_DISPATCH_SOURCE, digest)
    body = "\n".join(lines)
    check("the builder reads TRANSFORMS once, as dict(cls.TRANSFORMS)", body.count("TRANSFORMS") == 1 and "dict(cls.TRANSFORMS)" in body)

print("test_follow:", "FAILED %d" % failed if failed else "all checks pass")
sys.exit(1 if failed else 0)
