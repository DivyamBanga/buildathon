"""The child side: run chosen tests against one patched tree and report each verdict.

A child is forked from the coordinator, which has already imported sqlglot and
every test module at the pinned commit. It makes the patch real in one of two
ways and then runs tests with unittest, stopping each test at its first failure:

  hot    the patch only changes function bodies (and data files read while a
         test runs): the new code objects replace the old ones in place.
  clean  anything else: sqlglot and the tests are dropped from sys.modules and
         imported again from an overlay tree holding the patched files.

Messages to the coordinator, one per line on the pipe:
  S <index>            test started
  R <index> <0|1>      test finished, 1 = failed
  X <text>             the child could not do its job
  D                    all done
"""

from __future__ import annotations

import builtins
import importlib
import io
import os
import shutil
import sys
import unittest

POOL_WORKERS = 2


def warm_up(root: str, test_files: list[str]) -> None:
    """Import every test module, as pytest's collection does before any test runs."""
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)
    for path in test_files:
        importlib.import_module(path[:-3].replace("/", "."))


def build_overlay(root: str, destination: str, changes: dict) -> None:
    """A tree that looks like `root` with `changes` applied (path -> new text, None = removed).

    Only directories on the way to a changed file are real; everything else is a
    symlink into the read-only pinned checkout.
    """
    real_dirs = {""}
    for path in changes:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            real_dirs.add("/".join(parts[:depth]))
    for directory in sorted(real_dirs):
        os.makedirs(os.path.join(destination, directory), exist_ok=True)
    for directory in real_dirs:
        source_dir = os.path.join(root, directory)
        target_dir = os.path.join(destination, directory)
        prefix = directory + "/" if directory else ""
        try:
            names = os.listdir(source_dir)
        except OSError:
            names = []
        changed_stems = set()
        for path in changes:
            if path.startswith(prefix) and "/" not in path[len(prefix):] and path.endswith(".py"):
                changed_stems.add(path[len(prefix):-3])
        for name in names:
            relative = prefix + name
            if relative in real_dirs or relative in changes:
                continue
            if name == "__pycache__":
                # Compiled files of unchanged modules stay usable; a changed
                # module must be compiled again from its new source.
                cache_dir = os.path.join(target_dir, name)
                os.mkdir(cache_dir)
                for cached in os.listdir(os.path.join(source_dir, name)):
                    if cached.split(".", 1)[0] not in changed_stems:
                        os.symlink(
                            os.path.join(source_dir, name, cached), os.path.join(cache_dir, cached)
                        )
                continue
            os.symlink(os.path.join(source_dir, name), os.path.join(target_dir, name))
    for path, text in changes.items():
        if text is not None:
            with open(os.path.join(destination, path), "w", encoding="utf-8", newline="") as handle:
                handle.write(text)


def _install_redirects(redirects: dict) -> None:
    real_open = builtins.open

    def patched_open(file, *args, **kwargs):
        if type(file) is str:
            target = redirects.get(os.path.abspath(file))
            if target is not None:
                file = target
        elif isinstance(file, (bytes, os.PathLike)):
            try:
                target = redirects.get(os.path.abspath(os.fsdecode(file)))
            except Exception:
                target = None
            if target is not None:
                file = target
        return real_open(file, *args, **kwargs)

    builtins.open = patched_open
    io.open = patched_open


def _apply_swaps(swaps, function_index) -> None:
    for path, pairs in swaps:
        mapping = dict(pairs)
        for function in function_index.get(path, ()):
            new = mapping.get(function.__code__)
            if new is not None:
                function.__code__ = new


def _purge(root: str) -> None:
    prefix = root.rstrip("/") + "/"
    for name, module in list(sys.modules.items()):
        top = name.split(".", 1)[0]
        origin = getattr(module, "__file__", None) or ""
        if top in ("sqlglot", "tests", "sqlglotc") or origin.startswith(prefix):
            del sys.modules[name]
    sys.path[:] = [entry for entry in sys.path if entry.rstrip("/") != root.rstrip("/")]
    sys.path_importer_cache.clear()
    importlib.invalidate_caches()


def _patch_pools() -> None:
    """Make the tests' process pools fork from this (patched) process.

    The tests ask for spawned workers, which would import the unpatched tree in
    hot mode and cost a fresh interpreter each either way.
    """
    import concurrent.futures
    import multiprocessing

    fork = multiprocessing.get_context("fork")

    def get_context(method=None):
        return fork

    def pool(max_workers=None, mp_context=None, *args, **kwargs):
        return concurrent.futures.ProcessPoolExecutor(
            max_workers or POOL_WORKERS, fork, *args, **kwargs
        )

    for name, module in list(sys.modules.items()):
        if name.startswith("tests.") and module is not None:
            if getattr(module, "get_context", None) is not None:
                module.get_context = get_context
            if getattr(module, "ProcessPoolExecutor", None) is not None:
                module.ProcessPoolExecutor = pool


class _Result(unittest.TestResult):
    def __init__(self, emit) -> None:
        super().__init__()
        self.failfast = True  # a failing subtest ends its test at once
        self._emit = emit
        self._failed = False
        self.holders: list[str] = []
        self.started: list[int] = []

    def stop(self) -> None:
        # failfast must end the failing test, not the rest of the suite.
        pass

    def startTest(self, test) -> None:
        super().startTest(test)
        self._failed = False
        self.started.append(test._br_index)
        self._emit(f"S {test._br_index}\n")

    def stopTest(self, test) -> None:
        super().stopTest(test)
        self._emit(f"R {test._br_index} {1 if self._failed else 0}\n")

    def _bad(self, test) -> None:
        if hasattr(test, "_br_index"):
            self._failed = True
        else:
            # setUpClass / tearDownClass / class cleanup: no test owns it.
            self.holders.append(getattr(test, "description", str(test)))

    def addError(self, test, err) -> None:
        self._bad(test)

    def addFailure(self, test, err) -> None:
        self._bad(test)

    def addSubTest(self, test, subtest, err) -> None:
        if err is not None:
            self._failed = True

    def addUnexpectedSuccess(self, test) -> None:
        self._failed = True

    def addSuccess(self, test) -> None:
        pass

    def addSkip(self, test, reason) -> None:
        pass

    def addExpectedFailure(self, test, err) -> None:
        pass


def _run_tests(tests: list[tuple[int, str]], emit) -> None:
    groups: list[tuple[str, str, list[tuple[int, str]]]] = []
    for index, test_id in tests:
        path, class_name, method = test_id.split("::")
        module_name = path[:-3].replace("/", ".")
        if groups and groups[-1][0] == module_name and groups[-1][1] == class_name:
            groups[-1][2].append((index, method))
        else:
            groups.append((module_name, class_name, [(index, method)]))
    for module_name, class_name, members in groups:
        try:
            module = importlib.import_module(module_name)
            test_class = getattr(module, class_name)
        except BaseException:
            # The file cannot be imported: every test in it fails.
            for index, _ in members:
                emit(f"R {index} 1\n")
            continue
        cases = []
        for index, method in members:
            try:
                case = test_class(method)
            except BaseException:
                emit(f"R {index} 1\n")
                continue
            case._br_index = index
            cases.append(case)
        if not cases:
            continue
        _patch_pools()
        result = _Result(emit)
        unittest.TestSuite(cases).run(result)
        started = set(result.started)
        never = [case._br_index for case in cases if case._br_index not in started]
        for index in never:
            # setUpClass failed, so pytest reports an error for each of them.
            emit(f"R {index} 1\n")
        if result.holders and not never:
            # A class teardown error is reported against the last test that ran.
            emit(f"R {cases[-1]._br_index} 1\n")


def child_main(job, write_fd: int, root: str, function_index) -> None:
    """Runs in the forked child and never returns."""
    code = 0
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

        def emit(text: str) -> None:
            os.write(write_fd, text.encode())

        overlay = job["overlay"]
        changes = job["changes"]
        if changes:
            build_overlay(root, overlay, changes)
            # Any Python a test starts must see the patched tree too.
            os.environ["PYTHONPATH"] = overlay
        sys.dont_write_bytecode = True
        if job["mode"] == "clean":
            _purge(root)
            sys.path.insert(0, overlay)
            os.chdir(overlay)
        else:
            _apply_swaps(job["swaps"], function_index)
            if job["redirects"]:
                _install_redirects(
                    {path: os.path.join(overlay, os.path.relpath(path, root)) for path in job["redirects"]}
                )
        _run_tests(job["tests"], emit)
        emit("D\n")
    except BaseException as error:  # noqa: BLE001 - the coordinator decides what to do
        code = 1
        try:
            os.write(write_fd, f"X {type(error).__name__}: {error}\n".encode()[:500])
        except OSError:
            pass
    finally:
        try:
            if job.get("overlay"):
                shutil.rmtree(job["overlay"], ignore_errors=True)
        except BaseException:
            pass
        os._exit(code)
