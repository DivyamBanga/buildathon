"""Read the committed dependency map (tools/build_map.py, build_extra.py, build_cases.py).

Test sets are Python ints used as bitmaps: bit i is the i-th test of the map's
own tests.json. The coordinator translates bits to the run's test numbers.
"""

from __future__ import annotations

import json
import marshal
import os
import re
import zlib

_FAMILY = re.compile(r"^sqlglot/(dialects|parsers|generators)/(\w+)\.py$")
_NOT_A_DIALECT = ("dialect", "base", "__init__")


class CovMap:
    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.ids: list[str] = self._json("tests.json")
        self.count = len(self.ids)
        self.all = (1 << self.count) - 1
        self._index: dict[str, list[int]] = self._json("cov.json")
        self._opens: dict[str, list[int]] = self._json("opens.json")
        self._funcs: dict | None = None
        self._warm: dict | None = None
        self._blob: bytes | None = None
        self._files: dict[str, dict[int, dict[int, int]]] = {}
        self._packs: dict[str, dict | None] = {}
        self._classes: dict | None = None
        self._families: dict | None = None
        self._constants: dict | None = None
        self._read_at_import: set = set()
        self._cases: dict | None = None
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

    def opened(self, path: str) -> int | None:
        numbers = self._opens.get(path)
        if numbers is None:
            return None
        result = 0
        for number in numbers:
            result |= 1 << number
        return result

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

    def _changed_constants(self, path: str, qualname: str, changed, inserted) -> set | None:
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
        if not names or not all(name.isupper() for name in names) or names & self._read_at_import:
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
        if self._classes is None:
            self._classes = self._optional_json("classes.json") or {}
            self._families = self._optional_json("families.json") or {}
        if "<locals>" in qualname or self._pack("uses.bin") is None or self._pack("mentions.bin") is None:
            return None
        kind = self._classes.get(path, {}).get(qualname)
        if kind is None or kind == "enum":
            # Not a class of sqlglot's, or one whose members are reached without being named.
            return None
        reach = None
        if kind == "expr":
            # Nested classes are reached through their outermost class's name.
            reach = self._named("uses.bin", f"{path}:{qualname}") | self._named(
                "mentions.bin", qualname.split(".", 1)[0]
            )
        elif kind == "dialect":
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

        `units` maps a source path to [(code firstlineno, lines or None)].
        None means every case: the test reaches the code even with no case, or
        the per-case traces cannot account for how it reaches it.
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
                table = cases.get(first)
                if table:
                    for line, bitmap in table.items():
                        if lines is None or line in lines:
                            wanted |= bitmap
        if not wanted:
            return None
        return set(self.bits(wanted))
