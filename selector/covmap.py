"""Read the committed dependency map (see tools/build_map.py for how it is made).

Test sets are Python ints used as bitmaps: bit i is the i-th test of the map's
own tests.json. `to_numbers` translates to the numbers of the run's tests.json.
"""

from __future__ import annotations

import json
import marshal
import os
import zlib


class CovMap:
    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.ids: list[str] = json.load(open(os.path.join(directory, "tests.json")))
        self.count = len(self.ids)
        self.all = (1 << self.count) - 1
        self._index: dict[str, list[int]] = json.load(open(os.path.join(directory, "cov.json")))
        self._opens: dict[str, list[int]] = json.load(open(os.path.join(directory, "opens.json")))
        self._funcs: dict | None = None
        self._warm: dict | None = None
        self._blob: bytes | None = None
        self._files: dict[str, dict[int, dict[int, int]]] = {}

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

    def lines_any_code(self, path: str, lines) -> int:
        """Tests that executed any of these lines, whichever code object owns them."""
        wanted = set(lines)
        result = 0
        for table in self.file(path).values():
            for line in wanted.intersection(table):
                result |= table[line]
        return result

    def opened(self, path: str) -> int | None:
        numbers = self._opens.get(path)
        if numbers is None:
            return None
        result = 0
        for number in numbers:
            result |= 1 << number
        return result

    def opened_under(self, prefix: str) -> int:
        result = 0
        for path, numbers in self._opens.items():
            if path.startswith(prefix):
                for number in numbers:
                    result |= 1 << number
        return result

    def func_span(self, path: str, first: int):
        if self._funcs is None:
            self._funcs = json.load(open(os.path.join(self.directory, "funcs.json")))
        return self._funcs.get(path, {}).get(str(first))

    def _load_warm(self) -> dict:
        if self._warm is None:
            self._warm = json.load(open(os.path.join(self.directory, "warm.json")))
        return self._warm

    def warm(self, path: str) -> frozenset:
        """Code objects (by first line) that ran while the test modules were imported."""
        return frozenset(self._load_warm().get(path, ()))

    def warm_files(self) -> list[str]:
        """Source files imported by the time every test module is imported."""
        return list(self._load_warm())

    def bits(self, bitmap: int) -> list[int]:
        result = []
        index = 0
        while bitmap:
            low = bitmap & -bitmap
            index = low.bit_length() - 1
            result.append(index)
            bitmap ^= low
        return result
