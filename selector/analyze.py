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
# Only these two trees are reproduced faithfully for a child: a test that
# resolves the real location of its own file ends up in the pinned checkout
# for anything else.
_OWN_TREES = ("sqlglot/", "tests/")


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
        # Candidates that come from code that runs at import (module and class
        # bodies, functions already called by then) and from everything else.
        # The first kind can be looked at more closely, see selector/state.py.
        self.import_candidates = 0
        self.rest_candidates = 0
        # False when an import-time change is outside sqlglot's own modules.
        self.probe_ok = True
        # (path, code firstlineno, lines or None): changed functions that ran at
        # import and are answered for by what they left behind. None for now: a
        # function that ran at import may also be what imports a module on
        # demand, and that is not something the state of the package shows.
        self.warm_units: list = []
        # Every test that imported a file whose import-time code changed, before
        # any narrowing; `narrowed` says that some were left out as unable to
        # see the change. That holds only while the file still imports: a class
        # body that raises takes every importer of the module with it.
        self.import_wide = 0
        self.narrowed = False
        # Tests selected without being run: what they read cannot be put in
        # front of them faithfully (a file outside sqlglot/ and tests/ that a
        # test finds through the real location of its own file).
        self.unverified = 0
        # The fewest tests a comparison of import state could leave for the
        # import-time changes; None once that is unknown for one of them.
        self.import_floor: int | None = 0
        # Tests that import a changed file but were ruled out as candidates. If the
        # patched file can no longer be imported they fail all the same, so the
        # runner must try importing their test files.
        self.import_check = 0
        # (absolute path, new text) of patched modules the coordinator never
        # imports: a hot child imports the new source when a test asks for it.
        self.lazy: list[tuple[str, str]] = []
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
        elif path.startswith("tests/") and (name.startswith("test_") or name.endswith("_test.py")):
            # pytest collects it, so it is imported next to the existing tests.
            if not pydiff.harmless_new_test_module(patch.new_text or ""):
                plan.select_all = True
                plan.note(f"new test file {path} does more than define tests")
        return
    if patch.new_text is None or old_path != path:
        plan.needs_clean = True
        plan.sliceable = False
        plan.probe_ok = False
        found = covmap.file_any(old_path) | (covmap.opened(old_path) or 0)
        plan.candidates |= found
        plan.rest_candidates |= found
        if not old_path.startswith(_OWN_TREES):
            plan.unverified |= found
        plan.note(f"removed or renamed {old_path}")
        return
    if os.path.basename(path) in _PYTEST_CONFIG:
        plan.select_all = True
        plan.note(f"pytest config {path}")
        return
    if not path.startswith(_OWN_TREES):
        # Python outside the two trees a child sees patched (sqlglotc/setup.py,
        # which a test runs through the real location of its own file): the
        # tests that execute or open it are selected as they are.
        found = covmap.file_any(path) | (covmap.opened(path) or 0)
        plan.candidates |= found
        plan.rest_candidates |= found
        plan.unverified |= found
        plan.note(f"{path}: outside sqlglot/ and tests/, {found.bit_count()} tests selected unchecked")
        return
    if not covmap.has_file(path):
        opened = covmap.opened(path)
        if opened:
            plan.candidates |= opened
            plan.rest_candidates |= opened
            plan.needs_clean = True
            plan.sliceable = False
        plan.note(f"{path}: not executed by any test")
        return
    absolute = os.path.join(root, path)
    try:
        if pydiff.coding_cookie(patch.new_text) != pydiff.coding_cookie(patch.old_text or ""):
            # The file is read as other text than what is compared here.
            raise ValueError("encoding declaration changed")
        # Compiled from bytes, as the import system reads it (an encoding
        # declaration in a comment counts).
        new_code = compile(
            patch.new_text.encode("utf-8", "surrogateescape"), absolute, "exec", dont_inherit=True
        )
    except (SyntaxError, ValueError, RecursionError, MemoryError, OverflowError):
        plan.needs_clean = True
        plan.sliceable = False
        plan.probe_ok = False
        plan.candidates |= covmap.file_any(path)
        plan.rest_candidates |= plan.candidates
        plan.note(f"{path}: does not compile")
        return
    old_code = load_old_code(absolute, patch.old_text)
    diff = pydiff.diff_code(old_code, new_code)
    if not diff.changed:
        plan.note(f"{path}: no code change")
        return
    if path.startswith("tests/") and not diff.isomorphic:
        prefix = path + "::"
        known = {test_id[len(prefix):] for test_id in covmap.ids if test_id.startswith(prefix)}
        if pydiff.only_new_definitions(patch.old_text, patch.new_text, known):
            plan.note(f"{path}: only new tests or helpers were added")
            return
    old_lines = patch.old_text.splitlines(keepends=True)
    new_lines = patch.new_text.splitlines(keepends=True)
    warm = covmap.warm(path)
    hot = diff.isomorphic and absolute in loaded
    units = plan.units.setdefault(path, [])
    for old, new in diff.changed:
        first = old.co_firstlineno
        at_import = not pydiff.is_function_like(old)
        if not pydiff.is_function_like(old):
            # Module or class body: it ran at import, in every test that imported the file.
            hot = False
            found = covmap.code_any(path, first)
            plan.import_wide |= found
            units.append((first, None))
            narrowed = None
            same_names = new is not None and old.co_names == new.co_names
            if old.co_name == "<module>":
                if same_names and len(diff.changed) == 1:
                    # Nothing but this module's own top-level code changed.
                    narrowed = covmap.module_candidates(
                        path, found, patch.old_lines_changed, patch.pure_insert_after
                    )
            else:
                narrowed = covmap.class_candidates(
                    path,
                    old.co_qualname,
                    found,
                    patch.old_lines_changed,
                    patch.pure_insert_after,
                    same_names,
                )
            floor = covmap.best_case(
                path,
                "" if old.co_name == "<module>" else old.co_qualname,
                found,
                patch.old_lines_changed,
                patch.pure_insert_after,
            ) if new is not None and old.co_names == new.co_names else None
            if floor is None or plan.import_floor is None:
                plan.import_floor = None
            else:
                plan.import_floor |= floor
            if narrowed is not None:
                plan.note(
                    f"{path}:{first} {old.co_qualname}: {narrowed.bit_count()} tests can see it "
                    f"(of {found.bit_count()} that import it)"
                )
                plan.import_check |= found & ~narrowed
                plan.narrowed = True
                found = narrowed
            else:
                plan.note(f"{path}:{first} {old.co_name} import-time, {found.bit_count()} tests")
        else:
            if covmap.is_loader(path, first):
                # It imports modules on demand: when it runs depends on which
                # tests ran before, so a test run on its own says nothing here.
                plan.select_all = True
                plan.note(f"{path}:{first} {old.co_name} imports modules on demand")
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
        if at_import:
            plan.import_candidates |= found
            if not path.startswith("sqlglot/"):
                plan.probe_ok = False
        else:
            plan.rest_candidates |= found
    if hot:
        # Two different new versions for one old code object cannot be told apart in memory.
        seen: dict = {}
        for old, new in diff.pairs:
            if seen.setdefault(old, new) != new:
                hot = False
                break
    if absolute not in loaded:
        # Whatever changed in it, nothing of this module exists in the coordinator.
        plan.lazy.append((absolute, patch.new_text))
    elif hot:
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
    plan.rest_candidates |= opened
    if not old_path.startswith(_OWN_TREES):
        plan.unverified |= opened
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
        if text.strip():
            # Not a diff this parser knows: nothing can be said about it.
            plan.select_all = True
            plan.note("unrecognised patch")
        else:
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
