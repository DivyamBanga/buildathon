"""A set that notes which members are asked about: the stand-in of tools/trace_keys.py for set-valued attributes.

    RecSet(data, pairs)     data: a set or frozenset; `pairs` as for tools/recdict.RecDict

The notes go to tools/recdict.py's KEYS and WHOLE (the same registry, so tools/trace_keys.py
turns them into names the same way):

  KEYS   `m in s`, s.discard(m), s.remove(m)        one member asked about
  WHOLE  everything that depends on more than one member: iteration, len, bool, comparison,
         hash, repr, copy, pickle, the operators and methods that combine two sets, pop, ...

Why not a subclass of set or frozenset (as RecDict is a dict subclass)? CPython reads a set
that is passed as an ARGUMENT to a C function through its table, without calling any method
of the subclass: set(s), a.update(s), a | s with a a frozenset, a.issubset(s), a == s ... would go
unnoted. RecSet is therefore not a set at all: every one of those routes finds no set in `s`
and iterates it (or asks it for the reflected operator), which is noted as a whole read. What
this changes: type(s) is set / isinstance(s, (set, frozenset)) are False (isinstance(s,
collections.abc.Set) is True). tools/trace_keys.py's guard (executed lines and verdict with and
without the stand-ins) finds a test that notices; tools/test_recset.py checks each route.
"""

import collections.abc

import recdict


def _plain(value):
    """The set inside a RecSet (noting a whole read of it), or the value itself."""
    if isinstance(value, RecSet):
        recdict._whole(value)
        return value._data
    return value


class RecSet(collections.abc.Set):
    __slots__ = ("_data", "_br_pairs", "__weakref__")

    def __init__(self, data=(), pairs=()):
        self._data = data if isinstance(data, (set, frozenset)) else frozenset(data)
        self._br_pairs = tuple(pairs)
        recdict.TABLES[id(self)] = self

    # ---- one member -------------------------------------------------------------
    def __contains__(self, member):
        recdict._key(self, member)
        return member in self._data

    def discard(self, member):
        recdict._key(self, member)
        self._data.discard(member)

    def remove(self, member):
        recdict._key(self, member)
        self._data.remove(member)

    # ---- the whole set ----------------------------------------------------------
    def __iter__(self):
        recdict._whole(self)
        return iter(self._data)

    def __len__(self):
        recdict._whole(self)
        return len(self._data)

    def __bool__(self):
        recdict._whole(self)
        return bool(self._data)

    def __hash__(self):
        recdict._whole(self)
        return hash(self._data)

    def __repr__(self):
        recdict._whole(self)
        return repr(self._data)

    __str__ = __repr__

    def __format__(self, spec):
        return format(repr(self), spec)

    def __sizeof__(self):
        recdict._whole(self)
        return self._data.__sizeof__()

    def copy(self):
        recdict._whole(self)
        return self._data.copy()

    __copy__ = copy

    def __deepcopy__(self, memo):
        import copy

        recdict._whole(self)
        return copy.deepcopy(self._data, memo)

    def __reduce__(self):
        recdict._whole(self)
        return (type(self._data), (list(self._data),))

    def __reduce_ex__(self, protocol):
        return self.__reduce__()

    def __eq__(self, other):
        recdict._whole(self)
        return self._data == _plain(other)

    def __ne__(self, other):
        recdict._whole(self)
        return self._data != _plain(other)

    def __lt__(self, other):
        recdict._whole(self)
        return self._data < _plain(other)

    def __le__(self, other):
        recdict._whole(self)
        return self._data <= _plain(other)

    def __gt__(self, other):
        recdict._whole(self)
        return self._data > _plain(other)

    def __ge__(self, other):
        recdict._whole(self)
        return self._data >= _plain(other)

    def pop(self):
        recdict._whole(self)
        return self._data.pop()

    # operators: the plain set on the left or the right gets no say, RecSet answers both
    def __or__(self, other):
        recdict._whole(self)
        return self._data | _plain(other)

    def __ror__(self, other):
        recdict._whole(self)
        return _plain(other) | self._data

    def __and__(self, other):
        recdict._whole(self)
        return self._data & _plain(other)

    def __rand__(self, other):
        recdict._whole(self)
        return _plain(other) & self._data

    def __sub__(self, other):
        recdict._whole(self)
        return self._data - _plain(other)

    def __rsub__(self, other):
        recdict._whole(self)
        return _plain(other) - self._data

    def __xor__(self, other):
        recdict._whole(self)
        return self._data ^ _plain(other)

    def __rxor__(self, other):
        recdict._whole(self)
        return _plain(other) ^ self._data

    def __ior__(self, other):
        self._data |= _plain(other)
        return self

    def __iand__(self, other):
        recdict._whole(self)
        self._data &= _plain(other)
        return self

    def __isub__(self, other):
        recdict._whole(self)
        self._data -= _plain(other)
        return self

    def __ixor__(self, other):
        recdict._whole(self)
        self._data ^= _plain(other)
        return self

    def union(self, *others):
        recdict._whole(self)
        return self._data.union(*[_plain(other) for other in others])

    def intersection(self, *others):
        recdict._whole(self)
        return self._data.intersection(*[_plain(other) for other in others])

    def difference(self, *others):
        recdict._whole(self)
        return self._data.difference(*[_plain(other) for other in others])

    def symmetric_difference(self, other):
        recdict._whole(self)
        return self._data.symmetric_difference(_plain(other))

    def issubset(self, other):
        recdict._whole(self)
        return self._data.issubset(_plain(other))

    def issuperset(self, other):
        recdict._whole(self)
        return self._data.issuperset(_plain(other))

    def isdisjoint(self, other):
        recdict._whole(self)
        return self._data.isdisjoint(_plain(other))

    # changes: not reads
    def add(self, member):
        self._data.add(member)

    def clear(self):
        self._data.clear()

    def update(self, *others):
        self._data.update(*[_plain(other) for other in others])

    def intersection_update(self, *others):
        recdict._whole(self)
        self._data.intersection_update(*[_plain(other) for other in others])

    def difference_update(self, *others):
        recdict._whole(self)
        self._data.difference_update(*[_plain(other) for other in others])

    def symmetric_difference_update(self, other):
        recdict._whole(self)
        self._data.symmetric_difference_update(_plain(other))


_OWN: dict = {}  # id(table) -> {member: the member object the set holds}


def stored(table, key):
    """The member object the set itself holds for a lookup of `key` (equal members: 1 and True, a
    str enum member and its string), else `key`; the counterpart of recdict.stored."""
    own = _OWN.get(id(table))
    if own is None or len(own) != len(table._data):
        own = _OWN[id(table)] = {held: held for held in table._data}
    try:
        return own.get(key, key)
    except TypeError:
        return key
