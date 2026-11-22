"""What an import-time change leaves behind: compare the patched package with the pinned one.

A change to a module or class body runs once, when the module is imported.
Whatever it does, it does by leaving the program in a different state: other
module globals, other class attributes, other defaults or closures on the
functions. A test can only notice the change through that state.

So this module imports the whole sqlglot package twice in one process, once
from the pinned tree and once from the patched one, and compares the two
results object by object. Every difference is reported as a (module or class,
attribute) pair. No difference means the change cannot be observed. A few
differences tell which attributes a test would have to read to notice it; the
coordinator turns those into candidate tests (CovMap.state_candidates).

The comparison errs on the side of "different": two objects it does not know
how to compare are reported as a difference.
"""

from __future__ import annotations

import builtins
import enum
import functools
import importlib
import json
import logging
import os
import re
import shutil
import sys
import types
import typing
import warnings
import weakref

from . import pydiff

PACKAGE = "sqlglot"
# More differences than this are not worth narrowing by.
MAX_DIFFERENCES = 60

_MODULE_NOISE = frozenset(
    (
        "__builtins__",
        "__cached__",
        "__file__",
        "__spec__",
        "__loader__",
        "__path__",
        "__doc__",
        "__annotations__",
        "__warningregistry__",
    )
)
# Class attributes Python fills in from the source text or keeps for its own
# bookkeeping; none of them is read by sqlglot or its tests (see pydiff).
_CLASS_NOISE = frozenset(
    (
        "__module__",
        "__qualname__",
        "__doc__",
        "__dict__",
        "__weakref__",
        "__firstlineno__",
        "__static_attributes__",
        "__annotations__",
        "__classcell__",
        "_abc_impl",
    )
)
# What typing records about a TypedDict; such a class is a plain dict at run time.
_TYPED_DICT_NOISE = frozenset(
    ("__total__", "__required_keys__", "__optional_keys__", "__readonly_keys__", "__mutable_keys__")
)
_PLAIN = (int, float, complex, str, bytes, bool, type(None), type(Ellipsis), type(NotImplemented))
_BUILTIN_CALLABLES = (
    types.BuiltinFunctionType,
    types.MethodDescriptorType,
    types.WrapperDescriptorType,
    types.MethodWrapperType,
    types.ClassMethodDescriptorType,
    types.GetSetDescriptorType,
    types.MemberDescriptorType,
)
_LRU = type(functools.lru_cache(lambda: None))
_MISSING = object()
# (module, class qualname or "", attribute) -> the order of the tables in it is the same in every
# fresh import (maps/order.json, tools/build_order.py). Empty until load_order is called.
STABLE_ORDER: frozenset = frozenset()
_STATELESS = frozenset(("lock", "RLock", "_local", "local", "count", "object"))


def load_order(path: str) -> None:
    """Read the map of attributes whose tables come out in the same order in every fresh import."""
    global STABLE_ORDER
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        STABLE_ORDER = frozenset()
        return
    STABLE_ORDER = frozenset(
        (module, qualname, attribute)
        for module, classes in data.get("stable", {}).items()
        for qualname, attributes in classes.items()
        for attribute in attributes
    )


def package_modules(root: str) -> list[str]:
    """Every module of the package found under `root`, as dotted names."""
    names = []
    for directory, subdirs, files in os.walk(os.path.join(root, PACKAGE), followlinks=True):
        subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            parts = os.path.relpath(os.path.join(directory, name), root)[:-3].split(os.sep)
            if parts[-1] == "__main__":
                continue
            if parts[-1] == "__init__":
                parts.pop()
            names.append(".".join(parts))
    return names


def _forget() -> None:
    for name in list(sys.modules):
        if name.split(".", 1)[0] in (PACKAGE, "tests", "sqlglotc"):
            del sys.modules[name]
    sys.path_importer_cache.clear()
    importlib.invalidate_caches()


def _what(value) -> str:
    if isinstance(value, type):
        return f"class {value.__module__}.{value.__qualname__}"
    if isinstance(value, types.ModuleType):
        return f"module {value.__name__}"
    if isinstance(value, types.FunctionType):
        return f"function {value.__module__}.{value.__qualname__}"
    if value is None:
        return "None"
    return f"a {type(value).__module__}.{type(value).__qualname__}"


def on_demand() -> list:
    """Ask for what sqlglot imports on demand, the way running code does, and note what comes back.

    sqlglot loads most dialects when one is first named (attribute access on
    the package, Dialect.get, Dialect[...], Dialect.get_or_raise) and some of
    the optimizer the same way. Importing every module directly, as the rest of
    load_world does, never goes through that machinery, so a change that
    breaks it would leave two worlds that look alike. Here it is used on
    modules that are not imported yet; a different answer in the patched
    world (another class, None, an exception) shows in the returned list.
    """
    heard = []

    def ask(label, call):
        try:
            heard.append((label, _what(call())))
        except BaseException as error:  # noqa: BLE001 - what it raises is the answer
            heard.append((label, "raises " + type(error).__name__))

    dialects = sys.modules.get(PACKAGE + ".dialects.dialect")
    registry = getattr(dialects, "Dialect", None)
    names = [member.value for member in getattr(dialects, "Dialects", ()) if member.value]
    for index, name in enumerate(sorted(names)):
        if PACKAGE + ".dialects." + name in sys.modules or registry is None:
            continue
        # Each way of naming a dialect gets some that are not imported yet; a
        # fourth of them is left for the attribute access below.
        if index % 4 == 0:
            ask(f"Dialect.get({name})", lambda: registry.get(name))
        elif index % 4 == 1:
            ask(f"Dialect[{name}]", lambda: registry[name])
        elif index % 4 == 2:
            ask(f"Dialect.get_or_raise({name})", lambda: registry.get_or_raise(name))
    for module_name in sorted(name for name in sys.modules if name == PACKAGE or name.startswith(PACKAGE + ".")):
        module = sys.modules.get(module_name)
        if module is None or not callable(vars(module).get("__getattr__")):
            continue
        wanted = list(vars(module).get("__all__") or ())
        for value in list(vars(module).values()):
            # A table of names to the modules that hold them.
            if isinstance(value, dict) and value and all(
                isinstance(key, str) and key.isidentifier() and isinstance(target, (str, tuple))
                for key, target in value.items()
            ):
                wanted.extend(value)
        for attribute in dict.fromkeys(wanted):
            ask(f"{module_name}.{attribute}", lambda: getattr(module, attribute))
    return heard


def load_world(root: str, names: list[str], first: int, others: tuple = ()) -> tuple[dict, dict, list]:
    """Import the modules `names` from the tree at `root`, from scratch.

    The first `first` of them are imported directly (they are what importing
    the tests imports); then what sqlglot loads on demand is asked for
    (on_demand); then the rest is imported directly. Returns ({module name:
    module}, {module name: error type}, what on_demand heard). The modules stay
    alive in the returned dict after the next world replaces them in sys.modules.
    """
    _forget()
    drop = {os.path.normpath(entry) for entry in (root,) + tuple(others)}
    sys.path[:] = [entry for entry in sys.path if os.path.normpath(entry or ".") not in drop]
    sys.path.insert(0, root)
    failed = {}
    heard: list = []
    for index, name in enumerate(names):
        if index == first:
            heard = on_demand()
        try:
            importlib.import_module(name)
        except BaseException as error:  # noqa: BLE001 - whatever the module raises
            failed[name] = type(error).__name__
    if first >= len(names):
        heard = on_demand()
    modules = {
        name: module
        for name, module in sys.modules.items()
        if name == PACKAGE or name.startswith(PACKAGE + ".")
    }
    return modules, failed, heard


def outside_state() -> dict:
    """State an import can change that is not part of the package itself."""
    logger = logging.getLogger(PACKAGE)
    return {
        "recursion limit": sys.getrecursionlimit(),
        "environment": sorted(os.environ.items()),
        "warning filters": len(warnings.filters),
        # Also how many filters and handlers the package's logger and the root have: a filter
        # or a handler added at import decides which records the tests that watch the log get.
        "logging": (
            logging.root.level, logging.root.manager.disable, logger.level, logger.disabled, logger.propagate,
            len(logger.filters), len(logger.handlers), len(logging.root.filters), len(logging.root.handlers),
        ),
        "switch interval": sys.getswitchinterval(),
        # Every module finds the names it leaves to the builtins here.
        "the builtins": sorted((name, id(value)) for name, value in vars(builtins).items()),
    }


def token_of(value):
    """A hashable stand-in that is equal for equal members of the two worlds, or None."""
    kind = type(value)
    if kind in _PLAIN:
        return (kind.__name__, repr(value))
    if isinstance(value, enum.Enum):
        return ("enum", kind.__module__, kind.__qualname__, value.name)
    if isinstance(value, type):
        return ("class", value.__module__, value.__qualname__)
    if kind is tuple or kind is frozenset:
        parts = [token_of(item) for item in value]
        if any(part is None for part in parts):
            return None
        return (kind.__name__, tuple(parts) if kind is tuple else frozenset(parts))
    return None


def _flat(token):
    """A token as text that does not depend on the order of a frozenset."""
    if isinstance(token, tuple):
        return "(" + ",".join(_flat(part) for part in token) + ")"
    if isinstance(token, frozenset):
        return "{" + ",".join(sorted(_flat(part) for part in token)) + "}"
    return repr(token)


_OPAQUE = (
    type,
    enum.Enum,
    set,
    frozenset,
    types.ModuleType,
    types.CodeType,
    re.Pattern,
    logging.Logger,
    weakref.ref,
    range,
    slice,
    bytearray,
    functools.cached_property,
    _LRU,
    type({}.keys()),
    type({}.values()),
    type({}.items()),
    typing.TypeVar,
) + _BUILTIN_CALLABLES
_OPAQUE_MODULES = (
    "operator",
    "datetime",
    "decimal",
    "fractions",
    "uuid",
    "pathlib",
    "ipaddress",
    "typing",
    "types",
    "collections.abc",
    "_collections_abc",
)


def state_of(value):
    """What an instance holds (its attributes and slots), or None when it holds nothing."""
    state = {}
    found = False
    try:
        state.update(vars(value))
        found = True
    except TypeError:
        pass
    for cls in type(value).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for slot in slots:
            if slot in ("__dict__", "__weakref__"):
                continue
            found = True
            state[slot] = getattr(value, slot, _MISSING)
    return state if found else None


def held(item):
    """What an in-order comparison of `item` goes on to compare in order, or None where it stops.

    Tables, lists and tuples hold their entries; a function holds its defaults
    and what its closure holds; a partial, a method, a property and the like hold
    what they wrap; any other plain instance holds its attributes. Classes,
    modules, sets, enum members and values of the standard library hold nothing
    that is compared in order.
    """
    try:
        if isinstance(item, (dict, types.MappingProxyType)):
            return list(item.values())
        if isinstance(item, (list, tuple)):
            return list(item)
        if isinstance(item, types.FunctionType):
            out = list(item.__defaults__ or ()) + list((item.__kwdefaults__ or {}).values())
            for cell in item.__closure__ or ():
                try:
                    out.append(cell.cell_contents)
                except ValueError:
                    pass
            return out
        if isinstance(item, functools.partial):
            return [item.func, *item.args, *item.keywords.values()]
        if isinstance(item, types.MethodType):
            return [item.__func__, item.__self__]
        if isinstance(item, (staticmethod, classmethod)):
            return [item.__func__]
        if isinstance(item, property):
            return [item.fget, item.fset, item.fdel]
        if isinstance(item, types.CellType):
            try:
                return [item.cell_contents]
            except ValueError:
                return None
        if type(item) in _PLAIN or isinstance(item, _OPAQUE) or type(item).__module__ in _OPAQUE_MODULES:
            return None
        state = state_of(item)
        return None if state is None else list(state.values())
    except Exception:
        return None


def order_signature(value) -> str:
    """The order of every table and every run of classes inside `value`, as text.

    It walks what Comparer walks in order (`held`): dicts (by their keys when
    every key has a token), lists and tuples, and what functions, partials and
    instances hold; it records the keys of each dict and the members of each
    list or tuple that holds only classes. Empty when there is nothing whose
    order the comparison could look at.
    """
    rows: list = []
    seen: set = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) in _PLAIN or id(item) in seen:
            continue
        seen.add(id(item))
        children = held(item)
        if children is None:
            continue
        if isinstance(item, (dict, types.MappingProxyType)):
            tokens = [token_of(key) for key in item]
            if len(tokens) > 1 and all(token is not None for token in tokens):
                rows.append("d" + ";".join(_flat(token) for token in tokens))
        elif isinstance(item, (list, tuple)):
            if len(item) > 1 and all(isinstance(member, type) for member in item):
                rows.append("s" + ";".join(_flat(token_of(member)) for member in item))
        pending.extend(reversed(children))
    return "|".join(rows)


def iter_attributes(modules: dict):
    """(module, class qualname or "", attribute, value) as Comparer._namespace walks them."""
    for name, module in modules.items():
        stack = [("", dict(vars(module)), _MODULE_NOISE, "")]
        while stack:
            qualname, namespace, noise, prefix = stack.pop()
            for key, value in namespace.items():
                if key in noise:
                    continue
                if isinstance(value, type) and value.__module__ == name and value.__qualname__ == prefix + key:
                    inner = _CLASS_NOISE
                    if typing.is_typeddict(value):
                        inner = inner | _TYPED_DICT_NOISE
                    stack.append((value.__qualname__, dict(vars(value)), inner, value.__qualname__ + "."))
                    continue
                yield name, qualname, key, value


class Comparer:
    def __init__(self, old: dict, new: dict, old_root: str) -> None:
        self.old = old
        self.new = new
        self.old_root = old_root.rstrip("/") + "/"
        self.memo: dict = {}
        self.active: dict = {}
        self.low = 1 << 60
        self.keep: list = []
        self.differences: list = []
        self.changed_functions: dict = {}
        self.stable = STABLE_ORDER
        self.strict = False  # inside an attribute whose tables have a fixed order
        self.explain = False  # offline: remember where two values first differed
        self.why = ""

    # ---- reporting -------------------------------------------------------

    def report(self, owner: tuple, attribute: str, reason: str, function=None, entries=None) -> None:
        kind, module, qualname = owner
        path = ""
        first = 0
        origin = getattr(self.old.get(module), "__file__", None) or ""
        owner_path = origin[len(self.old_root):] if origin.startswith(self.old_root) else ""
        if function is not None:
            code = function.__code__
            if code.co_filename.startswith(self.old_root):
                path = code.co_filename[len(self.old_root):]
                first = code.co_firstlineno
        # `entries`: for a table that differs entry by entry, the names of those entries (see changed_entries).
        self.differences.append([kind, module, qualname, attribute, reason, owner_path, path, first, entries])
        if self.explain:
            self.differences[-1].append(self.why)
            self.why = ""

    # ---- twins: the same thing in the two worlds ---------------------------

    @staticmethod
    def _resolve(world: dict, module_name: str, qualname: str):
        found = world.get(module_name)
        if found is None:
            return None
        for part in qualname.split("."):
            try:
                found = vars(found)[part]
            except (KeyError, TypeError):
                return None
        return found

    def twins(self, a, b) -> bool:
        """True for one class as the pinned and as the patched package define it."""
        if a is b:
            return True
        if not isinstance(a, type) or not isinstance(b, type):
            return False
        if a.__module__ != b.__module__ or a.__qualname__ != b.__qualname__:
            return False
        home_a = self._resolve(self.old, a.__module__, a.__qualname__) is a
        home_b = self._resolve(self.new, b.__module__, b.__qualname__) is b
        if home_a and home_b:
            # Each is what its module calls by that name; their bodies are compared there.
            return True
        if home_a or home_b:
            return False
        # Classes made on the fly: compared by what they are made of.
        return (
            self.same(type(a), type(b))
            and self._pairs(a.__bases__, b.__bases__)
            and self._mapping(self._class_dict(a), self._class_dict(b), ordered=False)
        )

    @staticmethod
    def _class_dict(cls) -> dict:
        return {key: value for key, value in vars(cls).items() if key not in _CLASS_NOISE}

    def _no(self, a, b, what: str) -> bool:
        if self.explain and not self.why:
            self.why = f"{what}: {type(a).__module__}.{type(a).__qualname__} {str(a)[:70]!r} / {str(b)[:70]!r}"
        return False

    # ---- equality across the two worlds ------------------------------------

    def same(self, a, b) -> bool:
        if a is b:
            return True
        ta, tb = type(a), type(b)
        if ta is not tb and not self.same(ta, tb):
            return self._no(a, b, "types")
        if ta in _PLAIN:
            return repr(a) == repr(b) if ta is float or ta is complex else a == b
        strict = self.strict and held(a) is not None
        key = (id(a), id(b), strict)
        known = self.memo.get(key)
        if known is not None:
            return known
        depth = self.active.get(key)
        if depth is not None:
            # Already being compared further up: equal unless that comparison says otherwise.
            self.low = min(self.low, depth)
            return True
        depth = len(self.active)
        self.active[key] = depth
        outer, self.low = self.low, 1 << 60
        outer_strict, self.strict = self.strict, strict
        try:
            result = self._same(a, b)
        except RecursionError:
            raise
        except Exception:
            result = False
        finally:
            self.strict = outer_strict
        del self.active[key]
        if not result:
            self._no(a, b, "differ")
        if not result or self.low >= depth:
            # A verdict that rests on no comparison still under way can be reused.
            self.memo[key] = result
            self.keep.append((a, b))
        self.low = min(outer, self.low)
        return result

    def _pairs(self, items_a, items_b) -> bool:
        return len(items_a) == len(items_b) and all(self.same(x, y) for x, y in zip(items_a, items_b))

    def _mapping(self, a, b, ordered: bool = True) -> bool:
        """Two dicts hold the same things under the same keys.

        The order of the entries is not compared when the keys can be matched
        up by what they are: sqlglot builds some of its tables by looping over
        sets of classes, so their order already differs from run to run.
        """
        if len(a) != len(b):
            return False
        if not ordered:
            return all(key in b and self.same(value, b[key]) for key, value in a.items())
        right: dict = {}
        for key, value in b.items():
            token = self._token(key)
            if token is None:
                right = None
                break
            right[token] = value
        if right is not None and len(right) == len(b):
            if self.strict and [self._token(key) for key in a] != list(right):
                # The order of this table is the same in every fresh import
                # (STABLE_ORDER), so another order is another table.
                return False
            for key, value in a.items():
                other = right.get(self._token(key), _MISSING)
                if other is _MISSING or not self.same(value, other):
                    return False
            return True
        return all(
            self.same(ka, kb) and self.same(va, vb) for (ka, va), (kb, vb) in zip(a.items(), b.items())
        )

    _token = staticmethod(lambda value: token_of(value))

    def changed_entries(self, a, b) -> list | None:
        """For two dicts (or two sets) that differ: the names of the keys (members)
        that one has and the other lacks, or that hold another value.

        None when that cannot be told entry by entry: another kind of container,
        or a key that has no name of its own. The order of the entries is not
        part of this: only something that walks the whole table can see it.
        """
        kind = type(a)
        if kind is not type(b) or kind not in (dict, set, frozenset):
            return None
        sides = []
        for table in (a, b):
            named: dict = {}
            for key in table:
                token = self._token(key)
                if token is None or token in named:
                    return None
                named[token] = (key, table[key] if kind is dict else key)
            sides.append(named)
        left, right = sides
        names: set = set()
        for token in list(left) + [token for token in right if token not in left]:
            if token in left and token in right and self.same(left[token][1], right[token][1]):
                continue
            for side in sides:
                if token in side:
                    # Every name an equal key could be looked up under (see key_names).
                    found = key_names(side[token][0])
                    if found is None:
                        return None
                    names |= found
        return sorted(names)

    def _members(self, a, b) -> bool:
        if len(a) != len(b):
            return False
        left: dict = {}
        for item in a:
            token = self._token(item)
            if token is None:
                return False
            left[token] = item
        if len(left) != len(a):
            return False
        for item in b:
            other = left.pop(self._token(item), _MISSING)
            if other is _MISSING or not self.same(other, item):
                return False
        return True

    def _same(self, a, b) -> bool:
        if isinstance(a, type):
            return self.twins(a, b)
        if isinstance(a, enum.Enum):
            return a.name == b.name and self.same(a.value, b.value)
        if isinstance(a, (tuple, list)):
            if not self._attributes(a, b, optional=True):
                return False
            if self._pairs(a, b):
                return True
            # A sequence of classes made from a set (for isinstance and the like)
            # comes out in a different order every run: compare what is in it.
            return (
                not self.strict
                and len(a) == len(b)
                and all(isinstance(item, type) for item in a)
                and all(isinstance(item, type) for item in b)
                and sorted(map(self._token, a)) == sorted(map(self._token, b))
            )
        if isinstance(a, dict):
            return self._mapping(a, b) and self._attributes(a, b, optional=True)
        if isinstance(a, types.MappingProxyType):
            return self._mapping(dict(a), dict(b))
        if isinstance(a, (set, frozenset)):
            return self._members(a, b)
        if isinstance(a, types.FunctionType):
            reason = self.function_difference(a, b)
            if reason in ("code", "defaults", "closure", "code+defaults", "code+closure"):
                # Held somewhere (a table entry, a partial, a closure): whoever
                # uses it runs it, so it is reported as a changed function and
                # not as a changed value of whatever holds it.
                self.changed_functions[id(a)] = (a, "function-code" if reason == "code" else "function-meta")
                return True
            return reason is None
        if isinstance(a, types.MethodType):
            return self.same(a.__func__, b.__func__) and self.same(a.__self__, b.__self__)
        if isinstance(a, _BUILTIN_CALLABLES):
            return (
                getattr(a, "__name__", None) == getattr(b, "__name__", None)
                and self.same(getattr(a, "__self__", None), getattr(b, "__self__", None))
                and self.same(getattr(a, "__objclass__", None), getattr(b, "__objclass__", None))
            )
        if isinstance(a, (staticmethod, classmethod)):
            return self.same(a.__func__, b.__func__)
        if isinstance(a, property):
            return self.same(a.fget, b.fget) and self.same(a.fset, b.fset) and self.same(a.fdel, b.fdel)
        if isinstance(a, functools.partial):
            return self.same(a.func, b.func) and self.same(a.args, b.args) and self.same(a.keywords, b.keywords)
        if isinstance(a, functools.cached_property):
            return self.same(a.func, b.func) and a.attrname == b.attrname
        if isinstance(a, _LRU):
            # What the cache holds cannot be seen, so a filled cache is "different".
            return (
                self.same(a.__wrapped__, b.__wrapped__)
                and a.cache_parameters() == b.cache_parameters()
                and a.cache_info().currsize == 0
                and b.cache_info().currsize == 0
            )
        if isinstance(a, types.ModuleType):
            return a.__name__ == b.__name__
        if isinstance(a, types.CodeType):
            return self.codes_same(a, b)
        if isinstance(a, types.CellType):
            return self._cells((a,), (b,))
        if isinstance(a, re.Pattern):
            return a.pattern == b.pattern and a.flags == b.flags
        if isinstance(a, logging.Logger):
            return a.name == b.name
        if isinstance(a, weakref.ref):
            return self.same(a(), b())
        if isinstance(a, (range, slice, bytearray)):
            return a == b
        if isinstance(a, (type({}.keys()), type({}.values()), type({}.items()))):
            return self._pairs(list(a), list(b))
        if type(a).__name__ == "_tuplegetter":
            # The descriptor behind a namedtuple field.
            return a.__reduce__()[1] == b.__reduce__()[1]
        if type(a).__module__ == "operator":
            return repr(a) == repr(b)
        if type(a).__module__ in ("datetime", "decimal", "fractions", "uuid", "pathlib", "ipaddress"):
            # Values: equal when they compare equal and print alike.
            return a == b and repr(a) == repr(b)
        if type(a).__module__ in ("typing", "types", "collections.abc", "_collections_abc"):
            # Type variables, generic aliases and the like: their text names what they are.
            return repr(a) == repr(b)
        if isinstance(a, typing.TypeVar):
            return repr(a) == repr(b)
        return self._attributes(a, b, optional=False)

    _state = staticmethod(lambda value: state_of(value))

    def _attributes(self, a, b, optional: bool) -> bool:
        """Compare what two instances hold. `optional`: a plain container has nothing more."""
        if optional and type(a) in (tuple, list, dict):
            return True
        state_a, state_b = self._state(a), self._state(b)
        if state_a is None or state_b is None:
            if optional and state_a is None and state_b is None:
                return True
            if state_a is None and state_b is None and type(a).__name__ in _STATELESS:
                return True
            return False
        if type(a).__name__ == "Field" and type(a).__module__ == "dataclasses":
            # The annotation a field was declared with is kept for introspection only.
            state_a = {key: value for key, value in state_a.items() if key != "type"}
            state_b = {key: value for key, value in state_b.items() if key != "type"}
        return self._mapping(state_a, state_b, ordered=False)

    def _cells(self, cells_a, cells_b) -> bool:
        if len(cells_a or ()) != len(cells_b or ()):
            return False
        for cell_a, cell_b in zip(cells_a or (), cells_b or ()):
            try:
                value_a = cell_a.cell_contents
            except ValueError:
                value_a = _MISSING
            try:
                value_b = cell_b.cell_contents
            except ValueError:
                value_b = _MISSING
            if (value_a is _MISSING) != (value_b is _MISSING):
                return False
            if value_a is not _MISSING and not self.same(value_a, value_b):
                return False
        return True

    def codes_same(self, a, b) -> bool:
        if a == b:
            return True
        if not pydiff.same_behaviour(a, b):
            return False
        inner_a = [const for const in a.co_consts if isinstance(const, types.CodeType)]
        inner_b = [const for const in b.co_consts if isinstance(const, types.CodeType)]
        return len(inner_a) == len(inner_b) and all(self.codes_same(x, y) for x, y in zip(inner_a, inner_b))

    def function_difference(self, a, b) -> str | None:
        """None when the two functions behave alike, else what sets them apart.

        "code" means the code and nothing else: a function whose code differs
        and whose defaults, closure or attributes differ as well is
        "code+defaults", "code+closure" or "code+attributes". The lines of a
        changed body say who runs the change; another default or captured
        value is seen by every call, whatever lines it runs.
        """
        if a.__name__ != b.__name__ or a.__qualname__ != b.__qualname__ or a.__module__ != b.__module__:
            return "name"
        prefix = "" if self.codes_same(a.__code__, b.__code__) else "code+"
        if not self.same(a.__defaults__, b.__defaults__) or not self.same(a.__kwdefaults__, b.__kwdefaults__):
            return prefix + "defaults"
        if not self._cells(a.__closure__, b.__closure__):
            return prefix + "closure"
        if not self._mapping(a.__dict__, b.__dict__, ordered=False):
            return prefix + "attributes"
        return "code" if prefix else None

    # ---- walking the namespaces --------------------------------------------

    @staticmethod
    def _function_of(value):
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        return value if isinstance(value, types.FunctionType) else None

    def _difference(self, a, b):
        """(reason, function) when the two values differ, else None."""
        if isinstance(a, property) and isinstance(b, property):
            if self.same(a.fset, b.fset) and self.same(a.fdel, b.fdel):
                a, b = a.fget, b.fget
        elif isinstance(a, functools.cached_property) and isinstance(b, functools.cached_property):
            if a.attrname == b.attrname:
                a, b = a.func, b.func
        function_a, function_b = self._function_of(a), self._function_of(b)
        if function_a is not None and function_b is not None and type(a) is type(b):
            reason = self.function_difference(function_a, function_b)
            if reason is None:
                return None
            if reason == "code":
                return ("function-code", function_a)
            if reason in ("defaults", "closure", "code+defaults", "code+closure"):
                # Only a call reads these, and any call does.
                return ("function-meta", function_a)
            # Another name, or other attributes set on it: those can be read without calling it.
            return ("value", None)
        return None if self.same(a, b) else ("value", None)

    def _namespace(self, owner: tuple, a: dict, b: dict, module_name: str, prefix: str, noise) -> None:
        for key in list(a) + [key for key in b if key not in a]:
            if key in noise:
                continue
            if key not in a or key not in b:
                self.report(owner, key, "added" if key in b else "removed")
                continue
            value_a, value_b = a[key], b[key]
            if value_a is value_b:
                continue
            if (
                isinstance(value_a, type)
                and isinstance(value_b, type)
                and value_a.__module__ == module_name == value_b.__module__
                and value_a.__qualname__ == prefix + key == value_b.__qualname__
            ):
                self._class(value_a, value_b, module_name)
                continue
            self.strict = (owner[1], owner[2], key) in self.stable
            try:
                found = self._difference(value_a, value_b)
                # The entries are compared as the values were: where the order of
                # what an entry holds counts, another order makes it a changed entry.
                entries = self.changed_entries(value_a, value_b) if found is not None and found[0] == "value" else None
            finally:
                self.strict = False
            if found is not None:
                self.report(owner, key, found[0], found[1], entries)

    def _class(self, a: type, b: type, module_name: str) -> None:
        owner = ("class", module_name, a.__qualname__)
        if type(a) is not type(b) and not self.twins(type(a), type(b)):
            self.report(owner, "*", "metaclass")
        if not self._pairs(a.__bases__, b.__bases__):
            self.report(owner, "*", "bases")
        noise = _CLASS_NOISE
        if typing.is_typeddict(a) and typing.is_typeddict(b):
            noise = noise | _TYPED_DICT_NOISE
        self._namespace(owner, dict(vars(a)), dict(vars(b)), module_name, a.__qualname__ + ".", noise)

    def run(self) -> list:
        for name in list(self.old) + [name for name in self.new if name not in self.old]:
            a, b = self.old.get(name), self.new.get(name)
            if a is None:
                # A module only the patch has: nothing at the pinned commit imports it.
                continue
            if b is None:
                self.report(("module", name, ""), "*", "removed")
                continue
            if vars(a).get("__builtins__") is not vars(b).get("__builtins__"):
                # A module that binds its own builtins: every name its code leaves
                # to them may mean something else, and no line reads "__builtins__".
                self.report(("module", name, ""), "__builtins__", "value")
            self._namespace(("module", name, ""), dict(vars(a)), dict(vars(b)), name, "", _MODULE_NOISE)
        reported = {(row[6], row[7], row[4]) for row in self.differences}
        for function, reason in list(self.changed_functions.values()):
            code = function.__code__
            if not code.co_filename.startswith(self.old_root):
                # Code made at run time: nothing says who runs it.
                self.report(("module", function.__module__, ""), function.__qualname__, "value")
                continue
            key = (code.co_filename[len(self.old_root):], code.co_firstlineno, reason)
            if key not in reported:
                reported.add(key)
                self.report(("module", function.__module__, ""), function.__qualname__, reason, function)
        return self.differences


def _plain_key(key):
    """What a key is to a dict: the same for any two keys a dict would take for one.

    A dict finds an entry by hash and ==, and sqlglot has keys that are equal
    across types: a member of an enum that is also a str or an int equals
    its value, and 1 == True == 1.0. Returns None for a key that cannot be
    named, and for a class whose metaclass defines its own equality (a dialect
    class equals the string that names it).
    """
    kind = type(key)
    if isinstance(key, str) and (kind is str or isinstance(key, enum.Enum)):
        return ("str", repr(str.__str__(key)))
    if isinstance(key, (bool, int, float)) and (kind in (bool, int, float) or isinstance(key, enum.Enum)):
        number = float(key) if isinstance(key, float) else int(key)
        if isinstance(number, float) and number.is_integer():
            number = int(number)
        return ("num", repr(number))
    if kind is complex:
        return _plain_key(key.real) if key.imag == 0 else ("complex", repr(key))
    if kind in _PLAIN:
        return (kind.__name__, repr(key))
    if isinstance(key, enum.Enum):
        return ("enum", kind.__module__, kind.__qualname__, key.name)
    if isinstance(key, type):
        if type(key).__eq__ is not type.__eq__ or type(key).__hash__ is not type.__hash__:
            return None
        return ("class", key.__module__, key.__qualname__)
    if kind is tuple or kind is frozenset:
        parts = [_plain_key(item) for item in key]
        if any(part is None for part in parts):
            return None
        return (kind.__name__, tuple(parts) if kind is tuple else frozenset(parts))
    return None


def key_names(key) -> frozenset | None:
    """The names of a key of a table (dict, set): the one the comparison gives
    it, and the one any equal key shares with it (see _plain_key).

    Whoever looked a key up and whoever changed an entry mean the same entry
    when their names intersect. None: the key cannot be named, the table
    counts as a whole.
    """
    plain = _plain_key(key)
    token = token_of(key)
    if plain is None or token is None:
        return None
    return frozenset((token_text(token), token_text(plain)))


def token_text(token) -> str:
    """A token of Comparer._token as text, the same in every process."""
    if token[0] == "frozenset":
        return "frozenset(" + ",".join(sorted(token_text(part) for part in token[1])) + ")"
    if token[0] == "tuple":
        return "(" + ",".join(token_text(part) for part in token[1]) + ")"
    return ":".join(token)


def watchable(cls: type, name: str, value) -> bool:
    """True for a class attribute that is plain data: something code can only use by reading it.

    Such an attribute can be watched (tools/build_executor_memo.py puts a
    descriptor in its place that notes every read, through the class, a
    subclass or an instance). Not methods and other descriptors (using them
    runs their code), not the names Python itself looks up, not the members of
    an enum.
    """
    if name.startswith("__") and name.endswith("__"):
        return False
    if isinstance(cls, enum.EnumMeta):
        return False
    return not callable(value) and not hasattr(type(value), "__get__")


def watchable_pair(world: dict, module: str, qualname: str, name: str) -> bool:
    """The same for a (class, attribute) named as the comparison reports it, looked up in `world`
    (module name -> module). False when the class or the attribute is not there."""
    found = world.get(module)
    for part in qualname.split("."):
        try:
            found = vars(found)[part]
        except (KeyError, TypeError):
            return False
    if not isinstance(found, type) or name not in vars(found):
        return False
    return watchable(found, name, vars(found)[name])


def plan_changes(plan) -> dict:
    """path -> new text (None: removed) for every file the plan's patch changes."""
    changes: dict = {}
    for patch in plan.files:
        if patch.unapplied or patch.old_text == patch.new_text and patch.old_path in (None, patch.path):
            continue
        if patch.old_path and patch.old_path != patch.path:
            changes[patch.old_path] = None
        if patch.new_text is None:
            changes[patch.old_path or patch.path] = None
        else:
            changes[patch.path] = patch.new_text
    return changes


# The pinned package imported in full, kept by the coordinator for its children:
# (root, module names in import order, {name: module}, {name: error type}).
PINNED = None


def load_pinned(root: str) -> None:
    """Import the pinned package in full, a second time, next to the modules in use.

    The modules the process already uses are put back afterwards; the fresh
    copies are only ever compared against.
    """
    global PINNED
    if PINNED is not None and PINNED[0] == root:
        return
    loaded = [name for name in sys.modules if name == PACKAGE or name.startswith(PACKAGE + ".")]
    seen = set(loaded)
    names = loaded + [name for name in package_modules(root) if name not in seen]
    in_use = {
        name: module
        for name, module in sys.modules.items()
        if name.split(".", 1)[0] in (PACKAGE, "tests", "sqlglotc")
    }
    path = list(sys.path)
    try:
        modules, failed, heard = load_world(root, names, len(loaded))
    finally:
        _forget()
        sys.modules.update(in_use)
        sys.path[:] = path
        importlib.invalidate_caches()
    PINNED = (root, names, modules, failed, heard, len(loaded))


def compare_trees(old_root: str, new_root: str, loaded: list[str]) -> dict:
    """Import both trees and compare them. `loaded`: the modules to import first, in order."""
    import time

    clock = [time.process_time()]
    if PINNED is not None and PINNED[0] == old_root:
        _, names, old, old_failed, old_heard, first = PINNED
    else:
        seen = set(loaded)
        first = len(loaded)
        names = list(loaded) + [name for name in package_modules(old_root) if name not in seen]
        old, old_failed, old_heard = load_world(old_root, names, first, (new_root,))
    before = outside_state()
    clock.append(time.process_time())
    seen = set(names)
    names = names + [name for name in package_modules(new_root) if name not in seen]
    new, new_failed, new_heard = load_world(new_root, names, first, (old_root,))
    after = outside_state()
    clock.append(time.process_time())
    result: dict = {"differences": [], "inconclusive": "", "cpu": clock, "failed": [], "widen": False}
    if old_heard != new_heard:
        # Naming a module that is imported on demand no longer gives what it gave.
        result["inconclusive"] = "what is imported on demand changed"
        result["widen"] = True
        return result
    if old_failed != {name: kind for name, kind in new_failed.items() if name in seen}:
        result["inconclusive"] = "a module no longer imports"
        prefix = old_root.rstrip("/") + "/"
        for name in new_failed:
            origin = getattr(old.get(name), "__file__", None) or ""
            if name not in old_failed and origin.startswith(prefix):
                result["failed"].append(origin[len(prefix):])
        return result
    for key in before:
        if before[key] != after[key]:
            result["inconclusive"] = f"the import changed {key}"
            return result
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    comparer = Comparer(old, new, old_root)
    comparer.explain = bool(os.environ.get("BR_STATE_EXPLAIN"))
    try:
        result["differences"] = comparer.run()
    except RecursionError:
        result["inconclusive"] = "state too deep to compare"
    if len(result["differences"]) > MAX_DIFFERENCES:
        result["inconclusive"] = f"{len(result['differences'])} differences"
        result["differences"] = []
    clock.append(time.process_time())
    result["cpu"] = [round(later - earlier, 2) for earlier, later in zip(clock, clock[1:])]
    return result


def probe_main(job: dict, write_fd: int, root: str) -> None:
    """Runs in a forked child and never returns: one `E <json>` line, then `D`."""
    try:
        os.setpgid(0, 0)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        try:
            with open("/proc/self/oom_score_adj", "w") as handle:
                handle.write("1000")
        except OSError:
            pass
        from . import runner

        overlay = job["overlay"]
        runner.build_overlay(root, overlay, job["changes"])
        sys.dont_write_bytecode = True
        loaded = [name for name in sys.modules if name == PACKAGE or name.startswith(PACKAGE + ".")]
        result = compare_trees(root, overlay, loaded)
        os.write(write_fd, ("E " + json.dumps(result, separators=(",", ":")) + "\nD\n").encode())
    except BaseException as error:  # noqa: BLE001 - the coordinator decides what to do
        try:
            os.write(write_fd, f"X {type(error).__name__}: {error}\n".encode()[:500])
        except OSError:
            pass
    finally:
        try:
            shutil.rmtree(job.get("overlay", ""), ignore_errors=True)
        except BaseException:
            pass
        os._exit(0)
