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

# (loader kind, fixture file as the test names it) -> the pinned file's case
# numbers worth running. A fixture-driven test loops over independent cases;
# a case the patch cannot reach behaves as it did at the pinned commit, where
# it passed, so it is left out. Cases the patch itself adds or edits always run.
CASE_FILTER: dict = {}
# Calls made to the fixture loaders, for the offline tracer: (kind, file, cases).
LOADER_CALLS: list = []
# Called with (kind, file, index) just before a case is handed to the test.
CASE_HOOK = None

_redirects: dict = {}
_bypass_redirects = False
_pinned_fixtures = ""
_changed_fixtures: set = set()


def _cap_duckdb_threads() -> None:
    """Keep DuckDB to two threads per connection.

    It otherwise starts one per core of the host, and threads count against
    the container's limit of 256 processes.
    """
    try:
        import duckdb
    except Exception:
        return
    original = duckdb.connect
    if getattr(original, "_br_capped", False):
        return

    def connect(*args, **kwargs):
        if len(args) < 3:
            config = dict(kwargs.get("config") or {})
            config.setdefault("threads", 2)
            kwargs["config"] = config
        return original(*args, **kwargs)

    connect._br_capped = True
    duckdb.connect = connect


def warm_up(root: str, test_files: list[str]) -> None:
    """Import every test module, as pytest's collection does before any test runs."""
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)
    _cap_duckdb_threads()
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
    _redirects.update(redirects)

    def patched_open(file, *args, **kwargs):
        if not _bypass_redirects:
            if type(file) is str:
                target = _redirects.get(os.path.abspath(file))
                if target is not None:
                    file = target
            elif isinstance(file, (bytes, os.PathLike)):
                try:
                    target = _redirects.get(os.path.abspath(os.fsdecode(file)))
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


def _case_key(item):
    if isinstance(item, tuple):
        return tuple(tuple(sorted(part.items())) if isinstance(part, dict) else part for part in item)
    return item


def _wrap_loader(kind: str, original, helpers):
    def loader(filename):
        global _bypass_redirects
        allowed = CASE_FILTER.get((kind, filename))
        if allowed is None and CASE_HOOK is None:
            yield from original(filename)
            return
        items = list(original(filename))
        LOADER_CALLS.append((kind, filename, len(items)))
        if "tests/fixtures/" + filename in _changed_fixtures:
            # Number the cases as the pinned file has them; edited or added
            # cases have no number there and always run.
            saved = helpers.FIXTURES_DIR
            helpers.FIXTURES_DIR = _pinned_fixtures
            _bypass_redirects = True
            try:
                pinned = list(original(filename))
            finally:
                helpers.FIXTURES_DIR = saved
                _bypass_redirects = False
            numbers: dict = {}
            for index, item in enumerate(pinned):
                numbers.setdefault(_case_key(item), []).append(index)
            twins = [numbers.get(_case_key(item)) for item in items]
        else:
            twins = [[index] for index in range(len(items))]
        for item, indexes in zip(items, twins):
            if allowed is None or indexes is None or not allowed.isdisjoint(indexes):
                if CASE_HOOK is not None and indexes:
                    CASE_HOOK(kind, filename, indexes[0])
                yield item

    loader._br_wrapped = True
    return loader


# The TPC-H and TPC-DS tables TestExecutor.setUpClass parses from CSV, built
# once by the coordinator at the pinned commit (see build_executor_tables).
EXECUTOR_TABLES = None
# {"tpch_conn": {sql text: pickled DataFrame}, "tpcds_conn": {...}}: what DuckDB
# answers for the queries the executor tests send it (maps/duckdb.bin).
DUCKDB_ANSWERS = None


def load_duckdb_answers(path: str) -> None:
    global DUCKDB_ANSWERS
    import pickle
    import zlib

    try:
        with open(path, "rb") as handle:
            DUCKDB_ANSWERS = pickle.loads(zlib.decompress(handle.read()))
    except Exception:
        DUCKDB_ANSWERS = None


class _Answer:
    """Stands in for a DuckDB result whose rows are already known."""

    def __init__(self, connection, query: str, data: bytes) -> None:
        self._connection = connection
        self._query = query
        self._data = data

    def fetchdf(self):
        import pickle

        return pickle.loads(self._data)  # a fresh frame each time: callers rename columns in place

    df = fetchdf

    def __getattr__(self, name):
        # Anything else is asked of DuckDB itself.
        return getattr(self._connection.execute(self._query), name)


class _AnsweringConnection:
    """A DuckDB connection that does not recompute a query it has the answer to.

    The executor tests check sqlglot's own executor against DuckDB running the
    same query over the TPC tables. DuckDB's answer depends only on the exact
    SQL text it is given, so it is looked up by that text; any other text (for
    instance because the patch changed how the query is transpiled) is executed.
    """

    def __init__(self, connection, answers: dict) -> None:
        self._connection = connection
        self._answers = answers

    def execute(self, query, *args, **kwargs):
        if not args and not kwargs and type(query) is str:
            data = self._answers.get(query)
            if data is not None:
                return _Answer(self._connection, query, data)
        return self._connection.execute(query, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _NoConnection:
    def execute(self, *args, **kwargs):
        return self

    def close(self) -> None:
        pass


def build_executor_tables() -> None:
    """Run the executor tests' class setup once, without DuckDB, and keep its tables.

    Parsing the CSVs costs more than most executor tests. A child reuses these
    tables only for a patch that touches nothing the setup executes or reads
    (maps/setup.json), so they are exactly what its own setup would have built.
    """
    global EXECUTOR_TABLES
    import duckdb

    test_class = sys.modules["tests.test_executor"].TestExecutor
    real_connect = duckdb.connect
    duckdb.connect = lambda *args, **kwargs: _NoConnection()
    try:
        test_class.setUpClass()
    finally:
        duckdb.connect = real_connect
    EXECUTOR_TABLES = (test_class.tpch_tables, test_class.tpcds_tables)
    for name in ("tpch_conn", "tpcds_conn", "tpch_tables", "tpcds_tables", "cache", "tpch_sqls", "tpcds_sqls"):
        if name in test_class.__dict__:
            delattr(test_class, name)


def _use_executor_tables() -> None:
    """Make TestExecutor.setUpClass skip the CSV parsing and take the prebuilt tables."""
    module = sys.modules["tests.test_executor"]
    test_class = module.TestExecutor
    original = test_class.__dict__["setUpClass"].__func__
    tpch_tables, tpcds_tables = EXECUTOR_TABLES

    class _HeaderOnly:
        @staticmethod
        def reader(*args, **kwargs):
            return iter([[]])

    def setUpClass(cls):
        saved = module.csv, module.Table, module.open_file
        module.csv = _HeaderOnly
        module.open_file = lambda name: ()
        module.Table = lambda columns, rows: None
        try:
            original(cls)
        finally:
            module.csv, module.Table, module.open_file = saved
        cls.tpch_tables = dict(tpch_tables)
        cls.tpcds_tables = dict(tpcds_tables)
        if DUCKDB_ANSWERS:
            for name, answers in DUCKDB_ANSWERS.items():
                if name in cls.__dict__ and answers:
                    setattr(cls, name, _AnsweringConnection(getattr(cls, name), answers))

    test_class.setUpClass = classmethod(setUpClass)


# What sqlglot's own executor returns for the TPC queries at the pinned commit,
# keyed by the optimized query it was given (maps/execmemo.bin).
EXECUTOR_MEMO = None
# Which query set the running test executes ("tpch", "tpcds"), or None: the memo
# applies only inside the two tests it was recorded from.
MEMO_SCOPE = None
MEMO_TESTS = {"test_execute_tpch": "tpch", "test_execute_tpcds": "tpcds"}
# For the offline tools: a directory to record results into, and a callable
# told when the planning-and-execution phase starts ("X") and ends ("L").
MEMO_RECORD = None
MEMO_PHASE = None


def load_executor_memo(path: str) -> None:
    global EXECUTOR_MEMO
    import pickle
    import zlib

    try:
        with open(path, "rb") as handle:
            EXECUTOR_MEMO = pickle.loads(zlib.decompress(handle.read()))
    except Exception:
        EXECUTOR_MEMO = None


class _Planned:
    """Stands in for the plan of a query whose result is already known."""

    def __init__(self, key: str) -> None:
        self.key = key


def install_executor_memo() -> None:
    """Skip planning and executing a TPC query that is unchanged since the pinned commit.

    `sqlglot.executor.execute` optimizes the query, plans it and runs the plan.
    The plan and its result depend only on the optimized query and on the
    planner and executor code. This is installed only for a patch that touches
    none of that code (maps/execphase.json), so a query whose optimized form is
    exactly the pinned one must give the pinned result, which is returned
    without running it. Any other query is planned and executed as usual.
    """
    import hashlib
    import json
    import pickle

    import sqlglot.executor as executor
    from sqlglot import serde

    real_plan = executor.Plan
    real_executor = executor.PythonExecutor
    if getattr(real_plan, "_br_memo", False):
        return
    pending: dict = {}

    def plan(expression):
        scope = MEMO_SCOPE
        if scope is None:
            return real_plan(expression)
        if MEMO_PHASE is not None:
            MEMO_PHASE("X")
        try:
            text = json.dumps(serde.dump(expression), sort_keys=True, default=str)
            key = scope + ":" + hashlib.sha1(text.encode()).hexdigest()
        except Exception:
            return real_plan(expression)
        if MEMO_RECORD is None and EXECUTOR_MEMO is not None and key in EXECUTOR_MEMO:
            return _Planned(key)
        planned = real_plan(expression)
        pending["plan"], pending["key"] = planned, key
        return planned

    plan._br_memo = True

    class Executor:
        def __init__(self, *args, **kwargs) -> None:
            self._args = args
            self._kwargs = kwargs

        def execute(self, planned):
            try:
                if isinstance(planned, _Planned):
                    return pickle.loads(EXECUTOR_MEMO[planned.key])
                result = real_executor(*self._args, **self._kwargs).execute(planned)
                if MEMO_RECORD is not None and pending.get("plan") is planned:
                    name = os.path.join(MEMO_RECORD, f"{os.getpid()}-{pending['key'].replace(':', '-')}")
                    with open(name, "wb") as handle:
                        pickle.dump((pending["key"], pickle.dumps(result, protocol=4)), handle)
                return result
            finally:
                pending.clear()
                if MEMO_PHASE is not None and MEMO_SCOPE is not None:
                    MEMO_PHASE("L")

        def __getattr__(self, name):
            return getattr(real_executor(*self._args, **self._kwargs), name)

    executor.Plan = plan
    executor.PythonExecutor = Executor


# (module, attribute, case name): module-level collections a test loops over,
# each member an independent case, sliced like fixture cases (kind "members").
ENUMERATIONS = (("tests.test_generator", "Dialects", "Dialects@tests/test_generator.py"),)


class _Members:
    """Stands in for a collection a test loops over, leaving out filtered members."""

    def __init__(self, original, name: str) -> None:
        self._original = original
        self._name = name

    def __iter__(self):
        allowed = CASE_FILTER.get(("members", self._name))
        members = list(self._original)
        LOADER_CALLS.append(("members", self._name, len(members)))
        for index, member in enumerate(members):
            if allowed is None or index in allowed:
                if CASE_HOOK is not None:
                    CASE_HOOK("members", self._name, index)
                yield member

    def __getattr__(self, attribute):
        return getattr(self._original, attribute)

    def __call__(self, *args, **kwargs):
        return self._original(*args, **kwargs)

    def __getitem__(self, key):
        return self._original[key]

    def __contains__(self, item):
        return item in self._original

    def __len__(self):
        return len(self._original)


def _patch_test_modules() -> None:
    """Adapt the test modules' helpers to this process.

    Process pools: the tests ask for spawned workers, which would import the
    unpatched tree in hot mode and cost a fresh interpreter each either way, so
    they get workers forked from this (patched) process instead.

    Fixture loaders: wrapped so that CASE_FILTER can leave out cases.
    """
    import concurrent.futures
    import multiprocessing

    fork = multiprocessing.get_context("fork")

    def get_context(method=None):
        return fork

    class Pool(concurrent.futures.ProcessPoolExecutor):
        def __init__(self, max_workers=None, mp_context=None, *args, **kwargs):
            super().__init__(max_workers or POOL_WORKERS, fork, *args, **kwargs)

        def __exit__(self, exc_type, exc_value, traceback):
            if exc_type is not None:
                # The test has already failed: do not wait for the queued work.
                processes = list((self._processes or {}).values())
                self.shutdown(wait=False, cancel_futures=True)
                for process in processes:
                    try:
                        process.kill()
                    except Exception:
                        pass
                return False
            return super().__exit__(exc_type, exc_value, traceback)

    loaders = (("pairs", "load_sql_fixture_pairs"), ("lines", "load_sql_fixtures"))
    helpers = sys.modules.get("tests.helpers")
    if helpers is not None:
        for _kind, attribute in loaders:
            if not hasattr(helpers, "_br_" + attribute) and hasattr(helpers, attribute):
                setattr(helpers, "_br_" + attribute, getattr(helpers, attribute))
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "tests" or name.startswith("tests.")):
            continue
        if getattr(module, "get_context", None) is not None:
            module.get_context = get_context
        if getattr(module, "ProcessPoolExecutor", None) is not None:
            module.ProcessPoolExecutor = Pool
        if helpers is not None:
            for kind, attribute in loaders:
                original = getattr(helpers, "_br_" + attribute, None)
                if original is not None and getattr(module, attribute, None) is original:
                    setattr(module, attribute, _wrap_loader(kind, original, helpers))
    for module_name, attribute, name in ENUMERATIONS:
        module = sys.modules.get(module_name)
        current = getattr(module, attribute, None) if module is not None else None
        if current is None or isinstance(current, _Members):
            continue
        if ("members", name) in CASE_FILTER or CASE_HOOK is not None:
            setattr(module, attribute, _Members(current, name))


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
        global MEMO_SCOPE
        super().startTest(test)
        self._failed = False
        self.started.append(test._br_index)
        # Pool workers are forked inside the test and inherit this.
        MEMO_SCOPE = MEMO_TESTS.get(getattr(test, "_testMethodName", ""))
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


def run_tests(tests: list[tuple[int, str]], emit) -> None:
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
        _patch_test_modules()
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
    global _pinned_fixtures, _changed_fixtures
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
        _pinned_fixtures = os.path.join(root, "tests", "fixtures")
        _changed_fixtures = set(changes)
        CASE_FILTER.clear()
        CASE_FILTER.update(job.get("cases") or {})
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
            if job.get("executor_tables") and EXECUTOR_TABLES is not None:
                _use_executor_tables()
                if job.get("executor_memo") and EXECUTOR_MEMO:
                    install_executor_memo()
        for path in job.get("imports") or ():
            # pytest fails every test of a file it cannot import.
            try:
                importlib.import_module(path[:-3].replace("/", "."))
            except BaseException:
                emit(f"B {path}\n")
        run_tests(job["tests"], emit)
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
