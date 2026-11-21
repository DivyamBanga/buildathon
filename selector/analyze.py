"""Turn one patch into a plan: which tests could notice it, and how to check them.

The candidates come from the dependency map: a test is a candidate only when,
run alone at the pinned commit, it executed a changed statement or opened a
changed file. Candidates are then run against the patched code (see runner.py);
this module never decides that a test fails or passes.
"""

from __future__ import annotations

import importlib.util
import marshal
import os

from . import patchlib, pydiff
from .covmap import CovMap

# Files that configure pytest itself. The runner executes tests with unittest,
# so a change to how pytest is configured cannot be reproduced by running.
_PYTEST_CONFIG = ("pytest.ini", "tox.ini", ".pytest.ini", "conftest.py")
_MAYBE_PYTEST_CONFIG = ("pyproject.toml", "setup.cfg")


class Plan:
    def __init__(self, commit: str) -> None:
        self.commit = commit
        self.candidates = 0  # bitmap over the map's tests
        self.select_all = False  # could not be analysed: run every test
        self.needs_clean = False  # must be checked by importing the patched tree afresh
        self.files: list[patchlib.FilePatch] = []
        # (absolute path, [(old code, new code)]) for files whose functions can be swapped in place.
        self.swaps: list[tuple[str, list]] = []
        # absolute path -> new text, for data files read while a test runs.
        self.redirects: dict[str, str] = {}
        # source path -> [(code firstlineno, old lines or None)]: the changed code,
        # for working out which fixture cases reach it.
        self.units: dict[str, list] = {}
        # False when some change is not captured by `units` or a fixture edit.
        self.sliceable = True
        # Tests that import a changed file but were ruled out as candidates. If the
        # patched file can no longer be imported they fail all the same, so the
        # runner must try importing their test files.
        self.import_check = 0
        self.notes: list[str] = []

    def note(self, text: str) -> None:
        self.notes.append(text)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def load_old_code(absolute: str, old_text: str):
    """The pinned file's code object, from its .pyc when there is one."""
    try:
        cache = importlib.util.cache_from_source(absolute)
        with open(cache, "rb") as handle:
            data = handle.read()
        code = marshal.loads(data[16:])
        if code.co_filename == absolute:
            return code
    except Exception:
        pass
    return compile(old_text, absolute, "exec", dont_inherit=True)


def _analyze_python(plan: Plan, patch: patchlib.FilePatch, covmap: CovMap, root: str, loaded) -> None:
    path = patch.path
    old_path = patch.old_path or path
    if patch.old_text is None:
        # A new module. Nothing at the pinned commit imports it, so on its own it
        # reaches a test only through other changed files or through pytest.
        plan.needs_clean = True
        plan.sliceable = False
        name = os.path.basename(path)
        if name in _PYTEST_CONFIG or name in ("__init__.py", "sitecustomize.py", "usercustomize.py"):
            plan.select_all = True
            plan.note(f"new {path}")
        return
    if patch.new_text is None or old_path != path:
        plan.needs_clean = True
        plan.sliceable = False
        plan.candidates |= covmap.file_any(old_path)
        opened = covmap.opened(old_path)
        if opened:
            plan.candidates |= opened
        plan.note(f"removed or renamed {old_path}")
        return
    if os.path.basename(path) in _PYTEST_CONFIG:
        plan.select_all = True
        plan.note(f"pytest config {path}")
        return
    if not covmap.has_file(path):
        opened = covmap.opened(path)
        if opened:
            plan.candidates |= opened
            plan.needs_clean = True
            plan.sliceable = False
        plan.note(f"{path}: not executed by any test")
        return
    absolute = os.path.join(root, path)
    try:
        new_code = compile(patch.new_text, absolute, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError, MemoryError, OverflowError):
        plan.needs_clean = True
        plan.sliceable = False
        plan.candidates |= covmap.file_any(path)
        plan.note(f"{path}: does not compile")
        return
    old_code = load_old_code(absolute, patch.old_text)
    diff = pydiff.diff_code(old_code, new_code)
    if not diff.changed:
        plan.note(f"{path}: no code change")
        return
    old_lines = patch.old_text.splitlines(keepends=True)
    new_lines = patch.new_text.splitlines(keepends=True)
    warm = covmap.warm(path)
    hot = diff.isomorphic and absolute in loaded
    units = plan.units.setdefault(path, [])
    for old, new in diff.changed:
        first = old.co_firstlineno
        if not pydiff.is_function_like(old):
            # Module or class body: it ran at import, in every test that imported the file.
            hot = False
            found = covmap.code_any(path, first)
            units.append((first, None))
            narrowed = None
            if old.co_name != "<module>":
                narrowed = covmap.class_candidates(
                    path,
                    old.co_qualname,
                    found,
                    patch.old_lines_changed,
                    patch.pure_insert_after,
                    new is not None and old.co_names == new.co_names,
                )
            if narrowed is not None:
                plan.note(
                    f"{path}:{first} class {old.co_qualname}: {narrowed.bit_count()} tests use it "
                    f"(of {found.bit_count()} that import it)"
                )
                plan.import_check |= found & ~narrowed
                found = narrowed
            else:
                plan.note(f"{path}:{first} {old.co_name} import-time, {found.bit_count()} tests")
        else:
            lines = None
            if new is not None:
                span = covmap.func_span(path, first)
                if span is not None:
                    new_first = patch.new_line(first)
                    new_last = patch.new_line(span[1])
                    if new_first is not None and new_last is not None and new_last >= new_first:
                        lines = pydiff.changed_lines_in_function(
                            old_lines, new_lines, (first, span[1]), (new_first, new_last)
                        )
            units.append((first, lines))
            if lines is None:
                found = covmap.code_any(path, first)
                plan.note(f"{path}:{first} {old.co_name} whole function, {found.bit_count()} tests")
            else:
                found = covmap.code_lines(path, first, lines)
                plan.note(
                    f"{path}:{first} {old.co_name} lines {sorted(lines)}, {found.bit_count()} tests"
                )
            if first in warm:
                # It already ran while the test modules were imported, so state
                # built from the old version exists: only a fresh import is faithful.
                hot = False
        plan.candidates |= found
    if hot:
        # Two different new versions for one old code object cannot be told apart in memory.
        seen: dict = {}
        for old, new in diff.pairs:
            if seen.setdefault(old, new) != new:
                hot = False
                break
    if hot:
        plan.swaps.append((absolute, diff.pairs))
    else:
        plan.needs_clean = True


def _analyze_data(plan: Plan, patch: patchlib.FilePatch, covmap: CovMap, root: str) -> None:
    path = patch.path
    old_path = patch.old_path or path
    name = os.path.basename(path)
    if name in _PYTEST_CONFIG:
        plan.select_all = True
        plan.note(f"pytest config {path}")
        return
    if name in _MAYBE_PYTEST_CONFIG and "/" not in path:
        touched = "".join(
            row for hunk in patch.hunks for row in hunk[4] if row[:1] in "+-"
        ).lower()
        if "pytest" in touched or "unittest" in touched:
            plan.select_all = True
            plan.note(f"pytest config in {path}")
        else:
            plan.note(f"{path}: packaging config, no test reads it")
        return
    if patch.old_text is None and patch.new_file:
        plan.needs_clean = True
        plan.sliceable = False
        plan.note(f"new data file {path}")
        return
    opened = covmap.opened(old_path)
    if not opened:
        plan.note(f"{path}: no test opens it")
        return
    plan.candidates |= opened
    plan.note(f"{path}: opened by {opened.bit_count()} tests")
    if patch.new_text is None or old_path != path:
        plan.needs_clean = True
        plan.sliceable = False
    else:
        plan.redirects[os.path.join(root, path)] = patch.new_text


def analyze(commit: str, text: str, covmap: CovMap, root: str, loaded) -> Plan:
    """`loaded` holds the absolute paths of the modules already imported by the coordinator."""
    plan = Plan(commit)
    try:
        plan.files = patchlib.parse(text)
    except Exception as error:  # a malformed patch
        plan.select_all = True
        plan.note(f"unparsable patch: {error!r}")
        return plan
    if not plan.files:
        plan.note("empty patch")
        return plan
    for patch in plan.files:
        path = patch.path
        if not path or path.startswith("/") or ".." in path.split("/"):
            plan.select_all = True
            plan.note(f"odd path {path!r}")
            continue
        old_path = patch.old_path or path
        old_text = None
        if not patch.new_file:
            old_text = _read(os.path.join(root, old_path))
            if old_text is None and os.path.exists(os.path.join(root, old_path)):
                # Not text we can read: treat like a binary change.
                patch.binary = True
        if patch.binary:
            patch.unapplied = True
        else:
            try:
                patchlib.apply(patch, old_text)
            except Exception as error:
                patch.unapplied = True
                plan.note(f"{path}: apply raised {error!r}")
        if patch.unapplied:
            plan.select_all = True
            plan.note(f"{path}: patch does not apply")
            continue
        if old_text is None and not patch.new_file and patch.hunks:
            # The patch edits a file the pinned commit does not have.
            plan.select_all = True
            plan.note(f"{path}: missing at the pinned commit")
            continue
        if patch.old_text == patch.new_text and old_path == path:
            plan.note(f"{path}: unchanged")
            continue
        try:
            if path.endswith(".py") or old_path.endswith(".py"):
                _analyze_python(plan, patch, covmap, root, loaded)
            else:
                _analyze_data(plan, patch, covmap, root)
        except Exception as error:
            plan.select_all = True
            plan.note(f"{path}: analysis raised {error!r}")
    return plan
