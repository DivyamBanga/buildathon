"""Parse a git-style unified diff and apply it to the pinned source tree in memory.

Nothing here guesses: a hunk either applies exactly where its header says (or
at the single place its context matches) or the file is reported as
`unapplied`, and the caller falls back to its most conservative answer.
"""

from __future__ import annotations

import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")


class FilePatch:
    __slots__ = (
        "old_path",
        "new_path",
        "hunks",
        "binary",
        "new_file",
        "deleted",
        "old_text",
        "new_text",
        "unapplied",
        "copied",
        "old_lines_changed",
        "pure_insert_after",
        "_anchors",
    )

    def __init__(self) -> None:
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.hunks: list[tuple[int, int, int, int, list[str]]] = []
        self.binary = False
        self.new_file = False
        self.deleted = False
        self.old_text: str | None = None
        self.new_text: str | None = None
        self.unapplied = False
        # `copy from` / `copy to`: the hunks apply to the source's text, and the source stays.
        self.copied = False
        # 1-based old line numbers removed or replaced by the patch.
        self.old_lines_changed: list[int] = []
        # Old line numbers after which lines were inserted with no removal (0 = file start).
        self.pure_insert_after: list[int] = []
        # (first old line, new minus old) for each run of unchanged lines.
        self._anchors: list[tuple[int, int]] = [(1, 0)]

    def new_line(self, old_line: int) -> int | None:
        """Where an unchanged old line sits in the new file; None if it was changed."""
        if old_line in self.old_lines_changed:
            return None
        delta = 0
        for start, shift in self._anchors:
            if start > old_line:
                break
            delta = shift
        return old_line + delta

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


def _strip_prefix(path: str) -> str | None:
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    current: FilePatch | None = None
    lines = text.split("\n")
    index = 0
    count = len(lines)
    while index < count:
        line = lines[index]
        if line.startswith("diff --git "):
            current = FilePatch()
            files.append(current)
            match = _GIT_HEADER.match(line)
            if match:
                current.old_path = match.group(1)
                current.new_path = match.group(2)
            index += 1
            continue
        if current is None:
            if line.startswith("--- "):
                # A plain unified diff with no `diff --git` header.
                current = FilePatch()
                files.append(current)
            else:
                index += 1
                continue
        if line.startswith("new file mode"):
            current.new_file = True
        elif line.startswith("deleted file mode"):
            current.deleted = True
        elif line.startswith("rename from "):
            current.old_path = line[len("rename from "):].strip()
        elif line.startswith("rename to "):
            current.new_path = line[len("rename to "):].strip()
        elif line.startswith("copy from "):
            current.old_path = line[len("copy from "):].strip()
            current.copied = True
        elif line.startswith("copy to "):
            current.new_path = line[len("copy to "):].strip()
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
        elif line.startswith("--- "):
            old = _strip_prefix(line[4:].split("\t")[0])
            if old is None:
                current.new_file = True
            else:
                current.old_path = old
        elif line.startswith("+++ "):
            new = _strip_prefix(line[4:].split("\t")[0])
            if new is None:
                current.deleted = True
            else:
                current.new_path = new
        else:
            match = _HUNK.match(line)
            if match:
                old_start = int(match.group(1))
                old_count = int(match.group(2)) if match.group(2) is not None else 1
                new_start = int(match.group(3))
                new_count = int(match.group(4)) if match.group(4) is not None else 1
                body: list[str] = []
                index += 1
                seen_old = seen_new = 0
                while index < count and (seen_old < old_count or seen_new < new_count):
                    row = lines[index]
                    if row.startswith("\\"):
                        body.append(row)
                    elif row.startswith("+"):
                        seen_new += 1
                        body.append(row)
                    elif row.startswith("-"):
                        seen_old += 1
                        body.append(row)
                    else:
                        # A context line; an empty row is a context line whose
                        # single leading space was stripped.
                        seen_old += 1
                        seen_new += 1
                        body.append(row if row.startswith(" ") else " " + row)
                    index += 1
                if index < count and lines[index].startswith("\\"):
                    body.append(lines[index])
                    index += 1
                current.hunks.append((old_start, old_count, new_start, new_count, body))
                continue
        index += 1
    return files


def apply(patch: FilePatch, old_text: str | None) -> None:
    """Fill patch.old_text / new_text / old_lines_changed, or set patch.unapplied."""
    patch.old_text = old_text
    if patch.binary:
        patch.unapplied = True
        return
    if patch.deleted and not patch.hunks:
        patch.new_text = None
        return
    old = [] if not old_text else old_text.split("\n")
    old_ends_with_newline = not old_text or old_text.endswith("\n")
    if old_text and old_ends_with_newline:
        # `split` leaves an empty piece after the final newline.
        old.pop()
    new_ends_with_newline = old_ends_with_newline
    result: list[str] = []
    cursor = 0  # index into old of the first line not yet copied
    for old_start, old_count, _new_start, _new_count, body in patch.hunks:
        want_old = [row[1:] for row in body if row[:1] in (" ", "-")]
        start = old_start - 1 if old_count else old_start
        if old[start:start + len(want_old)] != want_old:
            # The header is off: accept the hunk only where it matches exactly once.
            places = [
                position
                for position in range(cursor, len(old) - len(want_old) + 1)
                if old[position:position + len(want_old)] == want_old
            ]
            if len(places) != 1:
                patch.unapplied = True
                return
            start = places[0]
        if start < cursor:
            patch.unapplied = True
            return
        result.extend(old[cursor:start])
        position = start
        removed_run: list[int] = []
        added_run = 0
        last_kind = " "
        for row in body:
            kind = row[:1]
            if kind == "\\":
                # "\ No newline at end of file" describes the row before it. After a
                # removed row it says the old file had no final newline, so whatever
                # the new side ends with does, unless a later marker says otherwise.
                new_ends_with_newline = last_kind == "-"
                continue
            if kind == " ":
                if removed_run:
                    patch.old_lines_changed.extend(removed_run)
                elif added_run:
                    patch.pure_insert_after.append(position)
                if removed_run or added_run:
                    patch._anchors.append((position + 1, len(result) - position))
                removed_run = []
                added_run = 0
                result.append(row[1:])
                position += 1
            elif kind == "-":
                removed_run.append(position + 1)
                position += 1
            elif kind == "+":
                added_run += 1
                result.append(row[1:])
            last_kind = kind
        if removed_run:
            patch.old_lines_changed.extend(removed_run)
        elif added_run:
            patch.pure_insert_after.append(position)
        if removed_run or added_run:
            patch._anchors.append((position + 1, len(result) - position))
        cursor = position
    result.extend(old[cursor:])
    if patch.deleted:
        patch.new_text = None
        return
    new_text = "\n".join(result)
    if result and new_ends_with_newline:
        new_text += "\n"
    patch.new_text = new_text
