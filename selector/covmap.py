"""Read the committed dependency map (tools/build_map.py, build_extra.py, build_cases.py).

Test sets are Python ints used as bitmaps: bit i is the i-th test of the map's
own tests.json. The coordinator translates bits to the run's test numbers.
"""

from __future__ import annotations

import importlib.util
import json
import marshal
import os
import re
import zlib

from . import state

_FAMILY = re.compile(r"^sqlglot/(dialects|parsers|generators)/(\w+)\.py$")
_CONSTANT_LIKE = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
_NOT_A_DIALECT = ("dialect", "base", "__init__")
# What the import system takes for a module in a directory.
_MODULE_SUFFIXES = (".py", ".pyc", ".so", ".pyd")
_DISTRIBUTIONS = (".dist-info", ".egg-info", ".egg-link", ".egg", ".pth")


class CovMap:
    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.ids: list[str] = self._json("tests.json")
        self.count = len(self.ids)
        self.all = (1 << self.count) - 1
        self._index: dict[str, list[int]] = self._json("cov.json")
        self._opens: dict[str, list[int]] = self._json("opens.json")
        self._sources: dict[str, list[int]] = self._optional_json("sources.json") or {}
        self._funcs: dict | None = None
        self._tindex: dict | None = None
        self._tblob: bytes | None = None
        self._tfiles: dict[str, dict[int, dict[int, int]]] = {}
        self._warm: dict | None = None
        self._import_run: dict | None = None
        self._blob: bytes | None = None
        self._files: dict[str, dict[int, dict[int, int]]] = {}
        self._packs: dict[str, dict | None] = {}
        self._classes: dict | None = None
        self._families: dict | None = None
        self._constants: dict | None = None
        self._names: tuple | None = None
        self._read_at_import: set = set()
        self._cases: dict | None = None
        self._dirs: dict | None = None
        self._case_index: dict | None = None
        self._case_blob: bytes | None = None
        self._case_sources: dict[str, dict] = {}

    def _json(self, name: str):
        with open(os.path.join(self.directory, name), encoding="utf-8") as handle:
            return json.load(handle)

    def _optional_json(self, name: str):
        try:
            return self._json(name)
        except (OSError, ValueError):
            return None

    # ---- line coverage ---------------------------------------------------

    def has_file(self, path: str) -> bool:
        return path in self._index

    def file(self, path: str) -> dict[int, dict[int, int]]:
        """code firstlineno -> line -> bitmap, for one source file."""
        cached = self._files.get(path)
        if cached is not None:
            return cached
        table: dict[int, dict[int, int]] = {}
        entry = self._index.get(path)
        if entry is not None:
            if self._blob is None:
                with open(os.path.join(self.directory, "cov.bin"), "rb") as handle:
                    self._blob = handle.read()
            offset, length = entry
            firsts, lines, which, sets = marshal.loads(zlib.decompress(self._blob[offset:offset + length]))
            decoded = [int.from_bytes(raw, "little") for raw in sets]
            for first, line, set_index in zip(firsts, lines, which):
                table.setdefault(first, {})[line] = decoded[set_index]
        self._files[path] = table
        return table

    def file_any(self, path: str) -> int:
        """Every test that executed anything in the file."""
        result = 0
        for lines in self.file(path).values():
            for bitmap in lines.values():
                result |= bitmap
        return result

    def tests_of_file(self, path: str) -> int:
        """Every test whose id names this test file."""
        prefix = path + "::"
        result = 0
        for bit, test_id in enumerate(self.ids):
            if test_id.startswith(prefix):
                result |= 1 << bit
        return result

    def test_names(self) -> frozenset:
        """The last part of every test id: names a new test must not take."""
        return frozenset(test_id.split("::")[-1].split("[")[0] for test_id in self.ids)

    def code_any(self, path: str, first: int) -> int:
        result = 0
        for bitmap in self.file(path).get(first, {}).values():
            result |= bitmap
        return result

    def code_lines(self, path: str, first: int, lines) -> int:
        table = self.file(path).get(first)
        if not table:
            return 0
        result = 0
        for line in lines:
            bitmap = table.get(line)
            if bitmap:
                result |= bitmap
        return result

    # ---- what a test runs while the test itself runs (tcov.bin) -------------

    def has_test_time(self) -> bool:
        if self._tindex is None:
            self._tindex = self._optional_json("tcov.json") or {}
        return bool(self._tindex)

    def _tfile(self, path: str) -> dict[int, dict[int, int]]:
        cached = self._tfiles.get(path)
        if cached is not None:
            return cached
        table: dict[int, dict[int, int]] = {}
        entry = self._tindex.get(path) if self.has_test_time() else None
        if entry is not None:
            if self._tblob is None:
                with open(os.path.join(self.directory, "tcov.bin"), "rb") as handle:
                    self._tblob = handle.read()
            offset, length = entry
            firsts, lines, which, sets = marshal.loads(zlib.decompress(self._tblob[offset:offset + length]))
            decoded = [int.from_bytes(raw, "little") for raw in sets]
            for first, line, set_index in zip(firsts, lines, which):
                table.setdefault(first, {})[line] = decoded[set_index]
        self._tfiles[path] = table
        return table

    def test_time(self, path: str, first: int, lines=None) -> int:
        """Tests that run the code object (or the given lines of it) after sqlglot is imported."""
        table = self._tfile(path).get(first)
        if not table:
            return 0
        result = 0
        if lines is None:
            for bitmap in table.values():
                result |= bitmap
        else:
            for line in lines:
                result |= table.get(line, 0)
        return result

    def opened(self, path: str) -> int | None:
        numbers = self._opens.get(path)
        if numbers is None:
            return None
        result = 0
        for number in numbers:
            result |= 1 << number
        return result

    def source_readers(self, path: str) -> int:
        """Tests that open this Python source as a file (to read, parse or run it):
        its text is data to them, whichever of its lines are ever executed."""
        result = 0
        for number in self._sources.get(path, ()):
            result |= 1 << number
        return result

    def entry_listers(self, path: str, root: str) -> tuple[int, int]:
        """(tests that could notice a file appearing at `path` or vanishing from it,
        those of them that cannot be run faithfully).

        The entry changes the listing of the nearest directory of the pinned tree
        above it. Two kinds of test notice: those that list that directory
        themselves (`explicit` in dirs.json) and those whose process asked the
        import system for the module name the entry would have, at any level of
        the new path (`names`). With no map every test counts.
        """
        if self._dirs is None:
            self._dirs = self._optional_json("dirs.json") or {}
        table = self._dirs
        if not table:
            return self.all, 0
        parts = path.split("/")
        depth = len(parts) - 1
        while depth > 0 and not os.path.isdir(os.path.join(root, *parts[:depth])):
            depth -= 1
        everything = (self.all, 0 if path.startswith(("sqlglot/", "tests/")) else self.all)
        if self._shadows(parts, depth, root):
            # The import system finds the new entry before something that answers to
            # its name today, whoever asks; and pytest's own import hook asks for test
            # modules and plugins without the map seeing it.
            return everything
        found = 0
        unchecked = 0
        directory = "/".join(parts[:depth])
        for number in table["explicit"].get(directory, ()):
            found |= 1 << number
        # importlib.metadata scans each sys.path entry for distributions: only an entry
        # named like one is looked at (standard library code, not a map of any patch).
        if parts[depth].lower().endswith(_DISTRIBUTIONS):
            for number in table.get("metadata", {}).get(directory, ()):
                found |= 1 << number
        # A test whose trace did not end cleanly is taken to list and ask for everything.
        for number in table.get("broken", ()):
            found |= 1 << number
        for number in table["unchecked"].get(directory, ()):
            unchecked |= 1 << number
        if table["names"] is None:
            # Which names the tests ask the import system for is not recorded: an
            # entry that could be imported (a module, or a directory) counts for all.
            if not parts[-1].endswith(_MODULE_SUFFIXES) and depth == len(parts) - 1:
                return found, unchecked
            return everything
        # Module names the new entries could have, counted from each place that is on sys.path.
        for base in table["roots"]:
            below = parts if not base else parts[len(base.split("/")):] if path.startswith(base + "/") else None
            if below is None:
                continue
            first_new = max(depth - (len(base.split("/")) if base else 0), 0)
            for index in range(first_new, len(below)):
                part = below[index]
                if index == len(below) - 1:
                    if not part.endswith(_MODULE_SUFFIXES):
                        break
                    part = part.split(".", 1)[0]
                    if part == "__init__":
                        # `a/b/__init__.py` is the module `a.b`, the one that is asked for.
                        part = below[index - 1] if index else part
                        index -= 1
                name = ".".join(below[:index] + [part])
                if name in table["everywhere"]:
                    return everything
                for number in table["names"].get(name, ()):
                    found |= 1 << number
        if not path.startswith(("sqlglot/", "tests/")):
            # Outside the two trees a child sees patched.
            unchecked |= found
        return found, unchecked

    @staticmethod
    def _shadows(parts: list, depth: int, root: str) -> bool:
        """True when a new entry takes a name that something else answers to today.

        Three ways. A file with a module suffix beside a module of the tree that
        has the same stem under another suffix (`test_parser.so` beside
        `test_parser.py`: an extension module is found first). A new directory
        beside a module of that name (a package is found before a module). And a
        new name at the top of the checkout that a standard or installed module
        has (`xdist.py`, `duckdb.py`, a directory `json`): the checkout comes
        before them on the path. Removing an entry is not judged here.
        """
        if depth >= len(parts):
            return False
        first = parts[depth]  # the first component that the pinned tree does not have, or the file itself
        last = depth == len(parts) - 1
        if last and not first.endswith(_MODULE_SUFFIXES):
            return False
        stem = first.split(".", 1)[0] if last else first
        directory = os.path.join(root, *parts[:depth])
        if os.path.lexists(os.path.join(directory, first)):
            return False  # it is there already: an entry that vanishes, or an edit
        for suffix in _MODULE_SUFFIXES:
            if os.path.isfile(os.path.join(directory, stem + suffix)):
                return True
        try:
            for name in os.listdir(directory):
                # x.cpython-313-x86_64-linux-gnu.so and the like
                if name.split(".", 1)[0] == stem and name.endswith(_MODULE_SUFFIXES):
                    return True
        except OSError:
            pass
        if depth == 0 and stem.isidentifier():
            try:
                spec = importlib.util.find_spec(stem)
            except Exception:
                return True
            if spec is not None:
                origin = getattr(spec, "origin", None) or ""
                locations = list(getattr(spec, "submodule_search_locations", None) or ())
                inside = [where for where in [origin] + locations if where and os.path.abspath(where).startswith(os.path.abspath(root) + os.sep)]
                if not inside:
                    return True
        return False

    def opens_of(self, bit: int) -> list[str]:
        """Fixture files (relative to tests/fixtures/) the test opens."""
        prefix = "tests/fixtures/"
        return [
            path[len(prefix):]
            for path, numbers in self._opens.items()
            if path.startswith(prefix) and bit in numbers
        ]

    def func_span(self, path: str, first: int):
        if self._funcs is None:
            self._funcs = self._json("funcs.json")
        return self._funcs.get(path, {}).get(str(first))

    def _load_warm(self) -> dict:
        if self._warm is None:
            self._warm = self._json("warm.json")
        return self._warm

    def warm(self, path: str) -> frozenset:
        """Code objects (by first line) that ran while the test modules were imported."""
        return frozenset(self._load_warm().get(path, ()))

    def warm_files(self) -> list[str]:
        """Source files imported by the time every test module is imported."""
        return list(self._load_warm())

    def runs_at_import(self, path: str, first: int) -> bool:
        """True when the code object runs while the whole package is imported
        (tools/build_importrun.py), or when that is not known."""
        if self._import_run is None:
            self._import_run = self._optional_json("importrun.json") or {}
        if not self._import_run:
            return True
        return first in self._import_run.get(path, ())

    @staticmethod
    def bits(bitmap: int) -> list[int]:
        result = []
        while bitmap:
            low = bitmap & -bitmap
            result.append(low.bit_length() - 1)
            bitmap ^= low
        return result

    # ---- classes ---------------------------------------------------------

    def _pack(self, name: str) -> dict | None:
        if name not in self._packs:
            try:
                with open(os.path.join(self.directory, name), "rb") as handle:
                    self._packs[name] = marshal.loads(zlib.decompress(handle.read()))
            except (OSError, ValueError, EOFError, zlib.error):
                self._packs[name] = None
        return self._packs[name]

    def _named(self, pack: str, key: str) -> int:
        table = self._pack(pack)
        raw = table.get(key) if table else None
        return int.from_bytes(raw, "little") if raw else 0

    def attribute_readers(self, module: str, qualname: str, attribute: str) -> int | None:
        """Tests that read the attribute of the class once sqlglot is imported, when the
        read map (tools/trace_attrs.py) covers that pair: a data attribute in the class's
        own dict. None for a pair it does not cover (functions, properties, enum members,
        names with double underscores, classes it could not name)."""
        table = self._pack("areaders.bin")
        if not table:
            return None
        raw = table.get(f"{module}:{qualname}|{attribute}")
        if raw is None:
            return None
        if state.PINNED is not None and not state.watchable_pair(state.PINNED[2], module, qualname, attribute):
            # The map covers exactly what state.watchable accepts; anything else is not its to answer.
            return None
        return int.from_bytes(raw, "little")

    def _changed_constants(self, path: str, qualname: str, changed, inserted, derived_ok=False) -> set | None:
        """Names of the class-level constants the changed lines assign, or None
        when the lines are not all plain `NAME = value` statements of the class."""
        if self._constants is None:
            self._constants = self._optional_json("constants.json") or {}
            self._read_at_import = set(self._constants.get("read_at_import", ()))
        classes = self._constants.get("classes", {}).get(path, {})
        info = classes.get(qualname)
        if not info or self._pack("readers.bin") is None:
            return None
        first, last = info["span"]
        if any(first - 1 <= position <= last for position in inserted):
            return None
        nested = [other["span"] for name, other in classes.items() if name.startswith(qualname + ".")]
        names = set()
        for line in changed:
            if not first <= line <= last or any(a <= line <= b for a, b in nested):
                continue
            for start, end, name in info["assigns"]:
                if start <= line <= end:
                    names.add(name)
                    break
            else:
                return None
        # Only constants by convention: lower-case names are fields and methods,
        # reached in other ways than by name. And a constant that another class or
        # module body reads feeds state built at import.
        if not names or not all(name.isupper() for name in names):
            return None
        if names & self._read_at_import and not derived_ok:
            return None
        return names

    def module_candidates(self, path: str, importers: int, changed, inserted) -> int | None:
        """Tests that can see a change to module-level code, when every changed line
        of the file gives a module constant (`NAME = value`) a new value: those
        that execute a line reading one of the names. None otherwise."""
        if self._constants is None:
            self._constants = self._optional_json("constants.json") or {}
            self._read_at_import = set(self._constants.get("read_at_import", ()))
        assigns = self._constants.get("modules", {}).get(path)
        if not assigns or inserted or not changed or self._pack("readers.bin") is None:
            return None
        names = set()
        for line in changed:
            for start, end, name in assigns:
                if start <= line <= end:
                    names.add(name)
                    break
            else:
                return None
        if not all(name.isupper() for name in names) or names & self._read_at_import:
            return None
        reading = 0
        for name in names:
            reading |= self._named("readers.bin", name)
        return importers & reading

    def _class_kind(self, path: str, qualname: str) -> str | None:
        if self._classes is None:
            self._classes = self._optional_json("classes.json") or {}
            self._families = self._optional_json("families.json") or {}
        if "<locals>" in qualname or self._pack("uses.bin") is None or self._pack("mentions.bin") is None:
            return None
        return self._classes.get(path, {}).get(qualname)

    def _named_users(self, path: str, qualname: str) -> int:
        """Tests that built an instance of the class (or of a subclass) or executed a line naming it.

        A nested class is reached through the name of its outermost class.
        """
        return self._named("uses.bin", f"{path}:{qualname}") | self._named(
            "mentions.bin", qualname.split(".", 1)[0]
        )

    def _reach(self, path: str, qualname: str, kind: str) -> int | None:
        """Tests that can reach the class at all, or None when that is not known."""
        if kind == "expr":
            return self._named_users(path, qualname)
        if kind == "dialect":
            match = _FAMILY.match(path)
            affected = self._families.get("affected", {}).get(match.group(2)) if match else None
            if affected is not None:
                tops = self._families.get("tops", {})
                reach = 0
                for family in affected:
                    for folder in ("dialects", "parsers", "generators"):
                        member = f"sqlglot/{folder}/{family}.py"
                        for name in self._classes.get(member, {}):
                            reach |= self._named("uses.bin", f"{member}:{name}")
                    for name in tops.get(family, ()):
                        reach |= self._named("mentions.bin", name)
                return reach
        return None

    def is_loader(self, path: str, first: int) -> bool:
        """True for a function that imports modules on demand, or that reads a
        registry such a function fills (found by tools/build_extra.py)."""
        if self._constants is None:
            self._constants = self._optional_json("constants.json") or {}
            self._read_at_import = set(self._constants.get("read_at_import", ()))
        return first in self._constants.get("loaders", {}).get(path, ())

    def is_registry_input(self, path: str, first: int) -> bool:
        """True for code that builds what those functions read to find an entry:
        a module's top-level code (`first` 0) or a class body (its first line)."""
        if self._constants is None:
            self._constants = self._optional_json("constants.json") or {}
            self._read_at_import = set(self._constants.get("read_at_import", ()))
        return first in self._constants.get("registry_inputs", {}).get(path, ())

    def best_case(self, path: str, qualname: str, importers: int, changed, inserted) -> int | None:
        """The fewest tests that comparing import state could leave for a change
        to the body of class `qualname` (or of the module, for ""), when that is
        known before comparing: every changed line gives a constant a new value,
        so at best the tests that read one of those constants remain. None when
        it is not known (the comparison may then leave anything, even nothing).
        """
        if not self.has_test_time() or self._pack("treaders.bin") is None:
            return None
        if self._constants is None:
            self._constants = self._optional_json("constants.json") or {}
            self._read_at_import = set(self._constants.get("read_at_import", ()))
        if qualname:
            names = self._changed_constants(path, qualname, changed, inserted, derived_ok=True)
            kind = self._class_kind(path, qualname)
            reach = self._reach(path, qualname, kind) if kind in ("expr", "dialect") else None
        else:
            assigns = self._constants.get("modules", {}).get(path)
            names = set()
            for line in changed:
                for start, end, name in assigns or ():
                    if start <= line <= end:
                        names.add(name)
                        break
                else:
                    return None
            if inserted or not names or not all(name.isupper() for name in names):
                return None
            reach = None
        if not names:
            return None
        if qualname:
            # A value the map knows the readers of needs no reach.
            module = path[:-3].replace("/", ".").removesuffix(".__init__")
            exact = [self.attribute_readers(module, qualname, name) for name in names]
            if None not in exact:
                floor = 0
                for bitmap in exact:
                    floor |= bitmap
                return floor
        reading = 0
        for name in names:
            reading |= self._named("treaders.bin", name)
        return (importers if reach is None else reach) & reading

    def _name_map(self) -> tuple:
        """(names, globals by file, star importers by file, lower-case keys) from maps/names.json; names empty without the map.

        The keys are what an expression class is called once lower-cased (`Expr.key`): the
        lower-cased names of all classes defined in the tree and of all names it reads."""
        if self._names is None:
            data = self._optional_json("names.json") or {}
            importers: dict = {}
            for importer, targets in data.get("stars", {}).items():
                for target in targets:
                    importers.setdefault(target, []).append(importer)
            named = frozenset(data.get("named", ()))
            self._names = (
                named,
                {path: frozenset(names) for path, names in data.get("globals", {}).items()},
                importers,
                frozenset(name.lower() for name in named) | frozenset(name.lower() for name in data.get("classes", ())),
            )
        return self._names

    def unobservable(self, kind: str, owner_path: str, qualname: str, attribute: str, reason: str, shape) -> bool:
        """True for a name that the patch adds or removes and that nothing at the pinned commit can read.

        `shape` is what the state comparison says about the difference ("" for a
        plain name; "R" when the value is a class that registers itself or is
        found by walking a namespace, or a module no other namespace binds;
        "F" when the owner class has a base from outside the package; None for
        a row that carries no shape). Anything but "" stays visible, and so
        does everything the map does not prove unread (README, "Names that
        nothing reads"):

        - a dunder (Python looks those up itself: __reduce__, __deepcopy__, ...);
        - a name ending in `_sql` whose stem can be an expression key (the
          generators and the simplifier build their dispatch with dir() over the
          class, and transforms and the generator call getattr(self, key + "_sql"),
          with the key of an expression class: the lower-cased name of a class
          defined anywhere in the tree, or a name the tree reads);
        - any name some code loads or stores as an attribute, imports, or holds
          as a string constant anywhere (maps/names.json `named`);
        - for a module: a name the module's own code loads as a global (a
          builtin it uses included), or that a module star-importing it
          (directly or through other star imports) loads;
        - for a class: the same, in the file the class is in, since a class
          body loads names from its own namespace first; and only for classes
          of the package that are expression, dialect or plain classes.
        """
        if reason not in ("added", "removed") or attribute in ("", "*") or shape != "":
            return False
        named, reads, importers, keys = self._name_map()
        if not named or not owner_path.startswith("sqlglot/") or owner_path not in reads:
            return False
        if attribute in named or (attribute.startswith("__") and attribute.endswith("__")):
            return False
        if attribute.endswith("_sql") and attribute[:-4].lower() in keys:
            return False
        if kind == "class":
            if self._class_kind(owner_path, qualname) not in ("expr", "dialect", "other"):
                return False
        elif kind != "module":
            return False
        pending, seen = [owner_path], set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            if attribute in reads.get(path, ()):
                return False
            pending.extend(importers.get(path, ()))
        return True

    def moved_readers(self, module: str, qualname: str, attribute: str, reason: str) -> int | None:
        """Tests that read a class attribute the patch removes, or one it adds under a name an
        ancestor already defines (so that a read through the class finds another value),
        taken from maps/areaders.bin; None when that map cannot answer.

        - removed: the readers of that (class, attribute) pair. A test that reads it finds
          what an ancestor has, or nothing; no other test reads it;
        - added: the readers of the same name on every ancestor that defines it (reads of an
          attribute are credited to the class whose dict holds it, whichever subclass or
          instance the read went through), narrowed to the tests that can reach the class
          where the map of reaches knows them. A name no ancestor defines is not answered
          here: nobody has read it from this class yet (that is maps/names.json's question).
        """
        if state.PINNED is None:
            return None
        if reason == "removed":
            return self.attribute_readers(module, qualname, attribute)
        found = state.PINNED[2].get(module)
        for part in qualname.split("."):
            found = vars(found).get(part) if found is not None and hasattr(found, "__dict__") else None
        if not isinstance(found, type) or "<locals>" in qualname:
            return None
        readers = None
        for base in found.__mro__[1:]:
            if attribute in vars(base):
                exact = self.attribute_readers(base.__module__, base.__qualname__, attribute)
                if exact is None:
                    return None
                readers = (readers or 0) | exact
        return readers

    def state_candidates(self, result: dict, own=frozenset()) -> int | None:
        """Tests that can notice what an import-time change left behind.

        `result` is what selector/state.py found: the (module or class,
        attribute) pairs whose value differs once the patched package is
        imported. A test notices such a difference only by reading that
        attribute:

        - a function that kept its code but has other defaults or another
          closure: by calling it, so by executing its body;
        - a data attribute of a class that maps/areaders.bin covers: by reading
          it (exactly the tests that read it, with no reach to intersect);
        - a constant (a name in capitals) of a module or class: by executing a
          line that reads the name, in a test that imported the module, or
          that can reach the class;
        - anything else on a class: by reaching the class.

        `own` names the changed code objects the caller answers for itself, as
        (path, first line). None when the differences narrow nothing down.
        """
        if result.get("inconclusive") or not self.has_test_time() or self._pack("treaders.bin") is None:
            return None
        readers = self._pack("treaders.bin")
        if self._constants is None:
            self._constants = self._optional_json("constants.json") or {}
            self._read_at_import = set(self._constants.get("read_at_import", ()))
        loaders = self._constants.get("loaders", {})
        found = 0
        for kind, module, qualname, attribute, reason, owner_path, path, first, *rest in result["differences"]:
            if not owner_path or not self.has_file(owner_path):
                return None
            if self.unobservable(kind, owner_path, qualname, attribute, reason, rest[0] if rest else None):
                continue
            if kind == "class" and reason == "value":
                exact = self.attribute_readers(module, qualname, attribute)
                if exact is not None:
                    found |= exact
                    continue
            if kind == "class" and reason in ("added", "removed") and not (rest[0] if rest else None):
                exact = self.moved_readers(module, qualname, attribute, reason)
                if exact is not None:
                    if reason == "added":
                        # Only what can reach the class sees the new value.
                        class_kind = self._class_kind(owner_path, qualname)
                        reach = self._reach(owner_path, qualname, class_kind) if class_kind in ("expr", "dialect") else None
                        if reach is not None:
                            exact &= reach
                    found |= exact
                    continue
            if reason in ("function-meta", "function-code") and path:
                # A function is used by running it. What it did while sqlglot was
                # imported is part of the differences already.
                if reason == "function-meta" or (path, first) not in own:
                    if first in loaders.get(path, ()):
                        # It imports modules on demand: when it runs depends on what ran before.
                        found |= self.code_any(path, first)
                    else:
                        found |= self.test_time(path, first)
                continue
            importers = self.code_any(owner_path, 1)
            if not importers:
                importers = self.file_any(owner_path)
            if kind == "module":
                if attribute == "*":
                    return None
                reach = importers
            else:
                class_kind = self._class_kind(owner_path, qualname)
                reach = None
                if class_kind in ("expr", "dialect"):
                    reach = self._reach(owner_path, qualname, class_kind)
                elif class_kind == "other" and attribute.startswith("__") and attribute != "*":
                    # What Python calls on instances (__hash__, __init__, ...): needs an instance.
                    reach = self._named_users(owner_path, qualname)
                if reach is None:
                    reach = importers
            if reason in ("value", "added", "removed") and _CONSTANT_LIKE.match(attribute) and attribute in readers:
                reach &= self._named("treaders.bin", attribute)
            found |= reach
        return found

    def class_candidates(
        self, path: str, qualname: str, importers: int, changed=(), inserted=(), same_names: bool = False
    ) -> int | None:
        """Tests that can see a change to the body of class `qualname`, or None
        when nothing narrower than `importers` (every test that imported the file)
        is known.

        A class body only sets attributes of that class. Two things narrow who
        can see a change to one:

        - who can reach the class at all: an instance of it or of a subclass, or
          a line naming it (expression classes), or any use of the dialects built
          on it (the classes of a dialect);
        - when the change only gives constants (`NAME = value`) a new value, who
          executes a line that reads one of those names.
        """
        kind = self._class_kind(path, qualname)
        if kind is None or kind == "enum":
            # Not a class of sqlglot's, or one whose members are reached without being named.
            return None
        reach = self._reach(path, qualname, kind)
        names = self._changed_constants(path, qualname, changed, inserted) if same_names else None
        if reach is None and not names:
            return None
        result = importers if reach is None else reach
        if names:
            reading = 0
            for name in names:
                reading |= self._named("readers.bin", name)
            result &= reading
        return result

    # ---- fixture cases ---------------------------------------------------

    def _load_cases(self) -> dict:
        if self._cases is None:
            data = self._optional_json("cases.json") or {"pairs": {}, "independent": []}
            self._cases = data["pairs"]
            self._independent = {(bit, fixture) for bit, fixture in data["independent"]}
            self._case_index = self._optional_json("casesrc.json") or {}
            self._sliced_by_bit: dict[int, list[str]] = {}
            for key in self._cases:
                bit, fixture = key.split("@", 1)
                self._sliced_by_bit.setdefault(int(bit), []).append(fixture)
        return self._cases

    def case_info(self, bit: int, fixture: str) -> dict | None:
        return self._load_cases().get(f"{bit}@{fixture}")

    def sliced_fixtures(self, bit: int) -> list[str]:
        """Everything the test loops over case by case (fixture files and collections)."""
        self._load_cases()
        return self._sliced_by_bit.get(bit, [])

    def case_independent(self, bit: int, fixture: str) -> bool:
        self._load_cases()
        return (bit, fixture) in self._independent

    def _case_source(self, path: str) -> dict:
        cached = self._case_sources.get(path)
        if cached is not None:
            return cached
        self._load_cases()
        table: dict = {}
        entry = self._case_index.get(path)
        if entry is not None:
            if self._case_blob is None:
                with open(os.path.join(self.directory, "cases.bin"), "rb") as handle:
                    self._case_blob = handle.read()
            offset, length = entry
            packed = marshal.loads(zlib.decompress(self._case_blob[offset:offset + length]))
            for key, (zero_firsts, zero_lines, firsts, lines, which, sets) in packed.items():
                zero: dict[int, set[int]] = {}
                for first, line in zip(zero_firsts, zero_lines):
                    zero.setdefault(first, set()).add(line)
                decoded = [int.from_bytes(raw, "little") for raw in sets]
                cases: dict[int, dict[int, int]] = {}
                for first, line, set_index in zip(firsts, lines, which):
                    cases.setdefault(first, {})[line] = decoded[set_index]
                table[key] = (zero, cases)
        self._case_sources[path] = table
        return table

    def case_slice(self, bit: int, fixture: str, units: dict) -> set[int] | None:
        """The fixture cases through which the test can reach the changed code.

        `units` maps a source path to [(code firstlineno, lines or None)] and must
        hold only code this test executes. None means every case: the test
        reaches the code even with no case, or the per-case traces cannot
        account for how it reaches some of it.
        """
        key = f"{bit}@{fixture}"
        if self.case_info(bit, fixture) is None:
            return None
        wanted = 0
        for path, entries in units.items():
            found = self._case_source(path).get(key)
            if found is None:
                return None
            zero, cases = found
            for first, lines in entries:
                fixed = zero.get(first)
                if fixed and (lines is None or not fixed.isdisjoint(lines)):
                    return None
                seen = 0
                table = cases.get(first)
                if table:
                    for line, bitmap in table.items():
                        if lines is None or line in lines:
                            seen |= bitmap
                if not seen:
                    # The test runs this code, yet no single case accounts for it.
                    return None
                wanted |= seen
        if not wanted:
            return None
        return set(self.bits(wanted))
