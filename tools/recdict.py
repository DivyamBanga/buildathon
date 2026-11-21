"""A dict that notes which keys are read: the stand-in tools/trace_keys.py puts in place of a table.

    RecDict(data, pairs)    a dict with the contents of `data`, tagged with the pairs it stands for

Every way of reading a dict ends in one of two notes:

  KEYS   (id(table), key)   a lookup of one key: d[k], d.get(k), k in d, d.pop(k), d.setdefault(k)
  WHOLE  id(table)          anything that depends on more than one key: iteration, len, keys,
                            values, items, copy, equality, repr, `**d`, dict(d), pickling, ...

All methods of dict are overridden, so that C code which works on a dict subclass
(dict(d), d.update(d2), {**d}, f(**d), json.dumps, pickle, copy) takes its slow path
through them: CPython uses the direct fast path only when the type's __iter__
is dict's own (PyDict_Merge). What a subclass changes that no override can undo:

  type(d) is dict    False for a RecDict (isinstance(d, dict) stays True)
  eval / exec / LOAD_GLOBAL with the table as the namespace
  other holders of the original dict (a copy is made: tools/trace_keys.py moves every holder it can
  find, and leaves a table alone, "whole", when something else holds it)

tools/test_recdict.py checks each route. The note is the cheapest possible (one tuple into a
set); the keys are turned into tokens (selector.state.Comparer._token) when a test ends.
"""

KEYS: set = set()
WHOLE: set = set()
TABLES: dict = {}  # id(table) -> table, so that a note can be turned into pairs and a token later
# While a window is open (the executor's planning and execution of one query), its notes go here as well.
WINDOW_KEYS = None
WINDOW_WHOLE = None


def _key(table, key):
    note = (id(table), key)
    KEYS.add(note)
    window = WINDOW_KEYS
    if window is not None:
        window.add(note)


def _whole(table):
    note = id(table)
    WHOLE.add(note)
    window = WINDOW_WHOLE
    if window is not None:
        window.add(note)


class RecDict(dict):
    __slots__ = ("_br_pairs", "__weakref__")

    def __init__(self, data=(), pairs=()):
        dict.__init__(self, data)
        self._br_pairs = tuple(pairs)
        TABLES[id(self)] = self

    # ---- one key ------------------------------------------------------------

    def __getitem__(self, key):
        _key(self, key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        _key(self, key)
        return dict.get(self, key, default)

    def __contains__(self, key):
        _key(self, key)
        return dict.__contains__(self, key)

    def pop(self, key, *default):
        _key(self, key)
        return dict.pop(self, key, *default)

    def setdefault(self, key, default=None):
        _key(self, key)
        return dict.setdefault(self, key, default)

    # ---- the whole table ----------------------------------------------------

    def __iter__(self):
        _whole(self)
        return dict.__iter__(self)

    def __reversed__(self):
        _whole(self)
        return dict.__reversed__(self)

    def __len__(self):
        _whole(self)
        return dict.__len__(self)

    def keys(self):
        _whole(self)
        return dict.keys(self)

    def values(self):
        _whole(self)
        return dict.values(self)

    def items(self):
        _whole(self)
        return dict.items(self)

    def popitem(self):
        _whole(self)
        return dict.popitem(self)

    def copy(self):
        _whole(self)
        return dict(dict.items(self))

    def __copy__(self):
        _whole(self)
        return dict(dict.items(self))

    def __deepcopy__(self, memo):
        _whole(self)
        import copy

        return copy.deepcopy(dict(dict.items(self)), memo)

    def __eq__(self, other):
        _whole(self)
        return dict.__eq__(self, other)

    def __ne__(self, other):
        _whole(self)
        return dict.__ne__(self, other)

    def __repr__(self):
        _whole(self)
        return dict.__repr__(self)

    def __str__(self):
        _whole(self)
        return dict.__repr__(self)

    def __format__(self, spec):
        _whole(self)
        return format(dict.__repr__(self), spec)

    def __or__(self, other):
        _whole(self)
        return dict.__or__(self, other)

    def __ror__(self, other):
        _whole(self)
        return dict.__ror__(self, other)

    def __reduce_ex__(self, protocol):
        _whole(self)
        return (dict, (dict(dict.items(self)),))

    def __reduce__(self):
        _whole(self)
        return (dict, (dict(dict.items(self)),))

    def __sizeof__(self):
        _whole(self)
        return dict.__sizeof__(self)

    def __bool__(self):
        _whole(self)
        return dict.__len__(self) != 0

    # ---- changes: not reads, but what a later whole read sees -----------------

    def __ior__(self, other):
        dict.__ior__(self, other)
        return self

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        dict.__delitem__(self, key)

    def update(self, *args, **kwargs):
        dict.update(self, *args, **kwargs)

    def clear(self):
        dict.clear(self)


def open_window():
    global WINDOW_KEYS, WINDOW_WHOLE
    WINDOW_KEYS, WINDOW_WHOLE = set(), set()


def close_window():
    """The notes made since open_window: (set of (table id, key), set of table ids)."""
    global WINDOW_KEYS, WINDOW_WHOLE
    found = (WINDOW_KEYS or set(), WINDOW_WHOLE or set())
    WINDOW_KEYS = WINDOW_WHOLE = None
    return found
