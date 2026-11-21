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
  C <index>            the class of this test raised after its tests had run
  X <text>             the child could not do its job
  D                    all done
"""

from __future__ import annotations

import builtins
import importlib
import importlib.machinery
import importlib.util
import io
import os
import re
import shutil
import sys
import unittest

POOL_WORKERS = 2
# Process pools the tests start are replaced by work done in the test's own
# process (see _InlinePool) unless this is switched off.
INLINE_POOLS = os.environ.get("BR_POOL", "inline") == "inline"
# The children collect cyclic garbage far less often than Python's default.
# What costs time is not the young collections but the full ones, each walking
# a heap that only grows while a test builds its trees (23 of them in
# test_max_nodes by default, one with these thresholds: 14 CPU s instead of
# 19-22). Collection stays on, so cyclic garbage does not pile up without
# bound. Neither sqlglot nor its tests depend on when a collection happens:
# the tests that care call gc.collect() themselves.
GC_THRESHOLD = tuple(int(part) for part in os.environ.get("BR_GC", "500000,50,1000").split(",") if part)

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
    # runpy and friends read code through io.open_code. The import system has
    # its own reference to the real one, so imports are not affected.
    real_open_code = io.open_code

    def patched_open_code(path):
        if not _bypass_redirects and type(path) is str:
            target = _redirects.get(os.path.abspath(path))
            if target is not None:
                path = target
        return real_open_code(path)

    io.open_code = patched_open_code


def _keep_resolved_paths_in(overlay: str, root: str) -> None:
    """Make a path resolved through the overlay's symlinks stay inside the overlay.

    The overlay is a tree of links into the pinned checkout. A test that
    resolves its own location (`Path(__file__).resolve()`) would land in the
    pinned tree and, from there, reach pinned copies of patched files. In a
    real checkout a resolved path of the tree is still a path of the tree, so
    here a resolved path under the pinned checkout is given back as the same
    path under the overlay (pathlib resolves through os.path.realpath).
    """
    real = os.path.realpath
    prefix = root.rstrip("/") + "/"
    stem = root.rstrip("/")

    def realpath(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        try:
            text = os.fsdecode(result)
        except Exception:
            return result
        if text == stem or text.startswith(prefix):
            mapped = overlay.rstrip("/") + text[len(stem):]
            return mapped if isinstance(result, str) else os.fsencode(mapped)
        return result

    os.path.realpath = realpath


def _apply_swaps(swaps, function_index) -> None:
    for path, pairs in swaps:
        mapping = dict(pairs)
        for function in function_index.get(path, ()):
            new = mapping.get(function.__code__)
            if new is not None:
                function.__code__ = new


def _module_name(path: str, root: str) -> str:
    """The name a source file of the checkout is imported under."""
    name = os.path.relpath(path, root)[:-3].replace(os.sep, ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


class _PatchedLoader(importlib.machinery.SourceFileLoader):
    """Loads a module from text held in memory, under the path it has in the pinned tree."""

    def __init__(self, name: str, path: str, text: str) -> None:
        super().__init__(name, path)
        self._text = text

    def get_code(self, fullname):
        # From bytes, as the file itself would be read: an encoding declaration
        # in its first two lines is honoured, or makes the import fail.
        return compile(self._text.encode("utf-8", "surrogateescape"), self.path, "exec", dont_inherit=True)

    def get_source(self, fullname):
        return self._text


class _PatchedFinder:
    def __init__(self, sources: dict) -> None:
        self._sources = sources  # module name -> (path in the pinned tree, patched text)

    def find_spec(self, name, path=None, target=None):
        entry = self._sources.get(name)
        if entry is None:
            return None
        origin, text = entry
        return importlib.util.spec_from_file_location(name, origin, loader=_PatchedLoader(name, origin, text))


def _serve_patched(modules, root: str) -> None:
    """Have patched modules that were never imported here come from their new source.

    sqlglot imports most of its dialects only when one is first used, so the
    coordinator, which has imported sqlglot and the test modules, has never
    run them. Nothing of such a module exists in this process: importing the
    patched source when a test first asks for it is exactly what a fresh
    process would do, whatever the patch changes in it (function bodies, class
    bodies, module-level code). A module that is already imported cannot be
    treated this way, and the caller starts over with a clean import.
    """
    sources = {}
    for path, text in modules:
        name = _module_name(path, root)
        if name in sys.modules:
            raise RuntimeError(f"{name} is already imported")
        sources[name] = (path, text)
    if sources:
        sys.meta_path.insert(0, _PatchedFinder(sources))


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
        run = getattr(self._connection, "_run", None) or self._connection.execute
        return getattr(run(self._query), name)


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


# {"tpch_conn": {"statements": [...], "files": [...]}, ...}: the statements the
# tests' class setups send to each DuckDB connection they open at the pinned
# commit, with the checkout's path written as ROOT_MARK (maps/ducksetup.json).
DUCKDB_SETUPS = None
ROOT_MARK = "<ROOT>"


def load_duckdb_setups(path: str) -> None:
    global DUCKDB_SETUPS
    import json

    try:
        with open(path, encoding="utf-8") as handle:
            DUCKDB_SETUPS = json.load(handle)["connections"]
    except Exception:
        DUCKDB_SETUPS = None


def _only_reads(query: str) -> bool:
    """True for a single plain SELECT: it cannot change what a later query sees."""
    text = query.strip().rstrip(";").strip()
    if ";" in text:
        return False
    head = text.lstrip("( \t\r\n")[:6].upper()
    if not head.startswith(("SELECT", "WITH")):
        return False
    lowered = text.lower()
    return not any(word in lowered for word in ("nextval", "currval", "setseed", "into ", "pragma", "attach"))


# A setup statement that makes a view of a whole CSV file, and nothing else.
_CSV_VIEW = re.compile(r"\s*CREATE VIEW (\w+) AS\s+(SELECT \*\s+FROM READ_CSV\(.*\))\s*", re.DOTALL)
# Words of a query that could tell a table from a view of the same rows, or look at the catalog.
_NOT_FOR_TABLES = (
    "rowid", "information_schema", "duckdb_", "pragma", "sqlite_", "pg_catalog", "describe", "summarize",
    "current_schema", "current_setting", "table_info", "read_csv", "glob(", "getvariable", "main.", "memory.",
    "temp.", "system.", "backwards",
)
# setup statements (as sent) -> (connection on the tables, connection on the same tables in reverse row order), or None
_TABLE_COPIES: dict = {}


def _table_copies(connect, statements: tuple):
    """Two connections to tables holding what the setup's views show: rows as the files give them, and reversed."""
    if statements not in _TABLE_COPIES:
        copies = None
        views = [_CSV_VIEW.fullmatch(statement) for statement in statements]
        if views and all(views):
            try:
                forward = connect()
                for view in views:
                    forward.execute(f"CREATE TABLE {view.group(1)} AS {view.group(2)}")
                forward.execute("CREATE SCHEMA backwards")
                for view in views:
                    name = view.group(1)
                    forward.execute(f"CREATE TABLE backwards.{name} AS SELECT * FROM main.{name} ORDER BY rowid DESC")
                backward = forward.cursor()
                backward.execute("SET schema = 'backwards'")
                copies = (forward, backward)
            except Exception:
                copies = None
        _TABLE_COPIES[statements] = copies
    return _TABLE_COPIES[statements]


def _frame_from_tables(connect, statements: tuple, query: str):
    """DuckDB's answer to a plain SELECT over the setup's views, computed on tables; None when it cannot be vouched for.

    The views read gzip CSV files, anew for every query: fifty times the cost
    of the query itself. The tables hold exactly the rows the views show, in
    the same order and with the same types (they are made by the setup's own
    READ_CSV calls), so a query means the same on both. What DuckDB is free to
    vary between the two is the plan, and with it the order of rows where the
    query leaves it open and the last digits of floating-point sums. So the
    query is run twice, on the tables and on copies whose rows are stored in
    reverse: a result that depends on the order rows arrive in differs
    between the two, and then, or after any error, the views are asked.
    """
    lowered = query.lower()
    if any(word in lowered for word in _NOT_FOR_TABLES):
        return None
    copies = _table_copies(connect, statements)
    if copies is None:
        return None
    try:
        from pandas.testing import assert_frame_equal

        frame = copies[0].execute(query).fetchdf()
        other = copies[1].execute(query).fetchdf()
        # Exact but for the rounding of sums taken in another order.
        assert_frame_equal(frame, other, check_exact=False, rtol=1e-9, atol=0.0)
    except BaseException as error:  # noqa: BLE001 - whatever it is, the real connection will say
        if isinstance(error, (KeyboardInterrupt, SystemExit, MemoryError)):
            raise
        return None
    return frame


class _Frame:
    """Stands in for a DuckDB result whose frame was computed on the table copies."""

    def __init__(self, connection, query: str, frame) -> None:
        self._connection = connection
        self._query = query
        self._frame = frame

    def fetchdf(self):
        return self._frame.copy()  # a fresh frame each time: callers rename columns in place

    df = fetchdf

    def __getattr__(self, name):
        # Anything else is asked of DuckDB itself.
        return getattr(self._connection._run(self._query), name)


class _ProbingConnection:
    """Stands in for a DuckDB connection until DuckDB is really needed.

    What a connection holds is decided by the statements it was sent. As long
    as those are, one for one, the statements a pinned class setup sends (the
    same tables, the same views over the same unchanged files), nothing is
    opened and nothing is executed: at the pinned commit those statements
    succeed on a fresh connection, so they will here, whenever they are run.
    Once the whole setup has been sent, a query the record has DuckDB's answer
    for is given that answer (the executor tests' TPC queries, maps/duckdb.bin).
    Any other plain SELECT is answered from tables that hold what the setup's
    views show, when that answer can be vouched for (_frame_from_tables).

    Anything else, a statement that is not the setup's next one, a query that
    could not be answered that way, any other use of the connection, opens the real
    connection, sends it what was held back, and is passed on. After a
    statement that is neither recognised nor a plain SELECT nothing is
    answered from the record any more.

    This is checked on what is really sent, in this process, by whatever code
    the patch left: nothing is assumed about the patch.
    """

    def __init__(self, connect, setups: dict, roots: list) -> None:
        self._connect = connect
        self._setups = setups  # tuple of setup statements -> {query: pickled answer}
        self._roots = roots
        self._connection = None
        self._held: list = []  # statements not yet sent to DuckDB, as given
        self._sent: list | None = []  # the same with the checkout's path marked; None: no longer a known setup
        self._answers = None
        self._given: list = []  # the setup's statements, as given
        self._setup: tuple | None = None  # those statements once the whole setup is recognised

    def _real(self):
        """The real connection, brought to where this one stands."""
        connection = self._connection
        if connection is None:
            connection = self._connection = self._connect()
            held, self._held = self._held, []
            for query in held:
                connection.execute(query)
        return connection

    def _forget(self) -> None:
        self._sent = None
        self._answers = None
        self._setup = None

    def execute(self, query, *args, **kwargs):
        if args or kwargs or type(query) is not str:
            self._forget()
            return self._real().execute(query, *args, **kwargs)
        answers = self._answers
        if answers is not None:
            data = answers.get(query)
            if data is not None:
                return _Answer(self, query, data)
            if not _only_reads(query):
                self._forget()
            elif self._setup is not None:
                frame = _frame_from_tables(self._connect, self._setup, query)
                if frame is not None:
                    return _Frame(self, query, frame)
            return self._real().execute(query)
        sent = self._sent
        if sent is None:
            return self._real().execute(query)
        text = query
        for prefix in self._roots:
            text = text.replace(prefix, ROOT_MARK + "/")
        key = tuple(sent) + (text,)
        found = self._setups.get(key)
        if found is None and not any(len(other) > len(key) and other[: len(key)] == key for other in self._setups):
            # Not the next statement of a setup we know.
            self._sent = None
            return self._real().execute(query)
        sent.append(text)
        self._given.append(query)
        if found is not None:
            self._sent = None
            self._answers = found
            self._setup = tuple(self._given)
        if self._connection is None:
            self._held.append(query)
            return self
        return self._connection.execute(query)

    def _run(self, query):
        """Really execute a query that was answered from the record (the caller wants more than its frame)."""
        return self._real().execute(query)

    def close(self):
        self._setup = None
        if self._connection is not None:
            return self._connection.close()
        self._held = []
        self._forget()
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        # Any other use may depend on, or change, what the connection holds.
        self._forget()
        return getattr(self._real(), name)


# (setup statements, files they read, answers) for every recorded setup; built
# once by the coordinator (prepare_duck_probe) and inherited by its children.
_DUCK_TABLE: list = []


def prepare_duck_probe() -> bool:
    """Work out, once, which recorded setups can be recognised. True when there is one."""
    del _DUCK_TABLE[:]
    for name, setup in (DUCKDB_SETUPS or {}).items():
        statements = setup.get("statements")
        if not statements:
            continue
        answers = (DUCKDB_ANSWERS or {}).get(name) or {}
        if not all(_only_reads(query) for query in answers):
            # An answered statement that changes the connection would be skipped: recognise the setup only.
            answers = {}
        _DUCK_TABLE.append((tuple(statements), frozenset(setup.get("files", ())), answers))
    return bool(_DUCK_TABLE)


def _install_duck_probe(roots, changed) -> None:
    """Have every plain duckdb.connect() return a _ProbingConnection.

    `roots` are the paths the checkout is seen under (the overlay, the pinned
    tree); `changed` holds the files the patch replaces or removes. A setup
    that hands one of those to DuckDB is not looked for.
    """
    setups = {statements: answers for statements, files, answers in _DUCK_TABLE if files.isdisjoint(changed)}
    if not setups:
        return
    import duckdb

    connect = duckdb.connect
    if getattr(connect, "_br_probing", False):
        return
    prefixes = sorted({root.rstrip("/") + "/" for root in roots if root}, key=len, reverse=True)

    def probing_connect(*args, **kwargs):
        if args or kwargs:
            return connect(*args, **kwargs)
        return _ProbingConnection(connect, setups, prefixes)

    probing_connect._br_probing = True
    probing_connect._br_capped = True
    duckdb.connect = probing_connect


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
    _snapshot_pinned()
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
                if name in cls.__dict__ and answers and not isinstance(cls.__dict__[name], _ProbingConnection):
                    setattr(cls, name, _AnsweringConnection(getattr(cls, name), answers))

    test_class.setUpClass = classmethod(setUpClass)


# What sqlglot's own executor returns for the TPC queries at the pinned commit,
# keyed by the optimized query it was given (maps/execmemo.bin).
EXECUTOR_MEMO = None
# Which query set the running test executes ("tpch", "tpcds"), or None: the memo
# applies only inside the two tests it was recorded from.
MEMO_SCOPE = None
MEMO_TESTS = {"test_execute_tpch": "tpch", "test_execute_tpcds": "tpcds"}
# memo key -> (bit mask of the phase lines, bit mask of the attributes) that one
# query runs and reads when it is planned and executed, and the digest of its
# tree with type annotations if that execution asks a node for its type, else ""
# (maps/execqueries.bin).
EXECUTOR_QUERIES = None
# (phase lines, attributes) the patch changes, or None when it changes none:
# a recorded result is used only for a query whose own record touches neither.
MEMO_BLOCK = None
# True in a child that imports the patched tree afresh and may reuse recorded
# results: the memo is put in place when one of those two tests starts.
MEMO_AFTER_IMPORT = False
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


def read_executor_queries(path: str) -> dict:
    """maps/execqueries.bin as {memo key: (lines, attributes, typed digest or "")}, the masks as ints; {} when it is not there."""
    import marshal
    import zlib

    try:
        with open(path, "rb") as handle:
            raw = marshal.loads(zlib.decompress(handle.read()))
        return {
            key: (int.from_bytes(lines, "little"), int.from_bytes(attributes, "little"), typed)
            for key, (lines, attributes, typed) in raw.items()
        }
    except Exception:
        return {}


def _query_untouched(key: str, typed: str) -> bool:
    """True when the recorded result of this query stands for the tree at hand.

    The key says the tree is the recorded one but for its type annotations.
    When planning and executing this query asks a node for its type, the
    annotations must be the recorded ones too. And nothing the patch changes
    may be run or read by this query's planning and execution.
    """
    own = (EXECUTOR_QUERIES or {}).get(key)
    if own is None or (own[2] and own[2] != typed):
        return False
    block = MEMO_BLOCK
    return block is None or (not own[0] & block[0] and not own[1] & block[1])


class _Planned:
    """Stands in for the plan of a query until its executor decides whether the result is known."""

    def __init__(self, expression, digest: str, typed: str) -> None:
        self.expression = expression
        self.digest = digest  # of the tree without its type annotations: the key
        self.typed = typed  # of the tree with them


def tree_digests(root) -> tuple[str, str]:
    """Two digests of an expression tree, read off the nodes themselves: (without the type annotations, with them).

    Each covers everything a node holds: its class, every argument in order
    (absent, None and an empty list are three different things), comments and
    meta, and the type of every leaf value. It also covers how the nodes of
    the tree are wired: a node that sits in two places is written as a
    reference to its first occurrence, and a child whose parent, argument name
    or index is not the place it was found in is marked. Two trees with the
    same digest are the same data structure to any code that walks them. No
    sqlglot code is called (a patch cannot change what a digest says), only
    attributes are read.

    The second digest also covers the type annotation of every node, written
    by value: sqlglot hands the same type object to many nodes, and which
    nodes share one depends on what the process did before, not on the query.
    The first leaves the annotations out: it is the right one for code that
    never asks a node for its type.

    Raises ValueError for a tree that hangs under another node (what is above
    it is not part of the digest) and for a type nested absurdly deep.
    """
    import enum
    import hashlib

    if getattr(root, "parent", None) is not None:
        raise ValueError("not the root of its tree")
    plain = hashlib.sha1()
    full = hashlib.sha1()
    seen: dict = {}
    # (value, parent, argument name, index or depth, mode, typed); mode 0: a
    # plain value, 1: a child found at that place, 2: inside a type annotation
    # (by value, `index` counts the depth), 3: punctuation. `typed`: part of a
    # type annotation, so it goes into the second digest only.
    todo = [(root, None, None, None, 0, False)]
    while todo:
        value, parent, key, index, mode, typed = todo.pop()
        kind = type(value)
        if mode == 3:
            data = value
        elif value is None:
            data = b"N;"
        elif kind is bool:
            data = b"T;" if value else b"F;"
        elif kind is int:
            data = b"i%d;" % value
        elif kind is str:
            text = value.encode("utf-8", "surrogatepass")
            data = b"s%d:" % len(text) + text
        elif kind is float:
            data = ("f" + repr(value) + ";").encode()
        elif type(getattr(value, "args", None)) is dict and hasattr(value, "parent"):
            inner = 1
            depth = 0
            if mode == 2:
                inner = 2
                depth = index + 1
                if depth > 100:
                    raise ValueError("type nested too deep")
                data = b""
            else:
                ordinal = seen.get(id(value))
                if ordinal is not None:
                    data = b"@%d;" % ordinal
                    full.update(data)
                    if not typed:
                        plain.update(data)
                    continue
                seen[id(value)] = len(seen)
                data = b""
            data += ("E" + kind.__module__ + "." + kind.__qualname__).encode()
            if mode == 1:
                if value.parent is not parent:
                    data += b"!p%d" % seen.get(id(value.parent), -1)
                if value.arg_key != key:
                    data += ("!k" + repr(value.arg_key)).encode()
                if value.index != index:
                    data += ("!i" + repr(value.index)).encode()
            tasks: list = [(b"(", None, None, None, 3, typed)]
            for name, argument in value.args.items():
                tasks.append((name, None, None, None, 0, typed))
                if type(argument) is list:
                    tasks.append((b"[", None, None, None, 3, typed))
                    for position, item in enumerate(argument):
                        tasks.append((item, value, name, depth if inner == 2 else position, inner, typed))
                    tasks.append((b"]", None, None, None, 3, typed))
                else:
                    tasks.append((argument, value, name, depth if inner == 2 else None, inner, typed))
            tasks.append((b"|", None, None, None, 3, typed))
            annotation = getattr(value, "_type", None)
            if annotation is value:
                tasks.append((b"self;", None, None, None, 3, True))
            else:
                tasks.append((annotation, None, None, depth, 2, True))
            tasks.append((getattr(value, "comments", None), None, None, None, 0, typed))
            tasks.append((getattr(value, "_meta", None), None, None, None, 0, typed))
            tasks.append((b")", None, None, None, 3, typed))
            todo.extend(reversed(tasks))
        elif isinstance(value, enum.Enum):
            data = ("e" + kind.__module__ + "." + kind.__qualname__ + "." + value.name + ";").encode()
        elif kind is list or kind is tuple:
            tasks = [(b"[" if kind is list else b"<", None, None, None, 3, typed)]
            tasks.extend((item, None, None, index, 2 if mode == 2 else 0, typed) for item in value)
            tasks.append((b"]", None, None, None, 3, typed))
            todo.extend(reversed(tasks))
            continue
        elif kind is dict:
            tasks = [(b"{", None, None, None, 3, typed)]
            for name, item in value.items():
                tasks.append((name, None, None, None, 0, typed))
                tasks.append((item, None, None, index, 2 if mode == 2 else 0, typed))
            tasks.append((b"}", None, None, None, 3, typed))
            todo.extend(reversed(tasks))
            continue
        else:
            data = ("o" + kind.__module__ + "." + kind.__qualname__ + ":" + repr(value) + ";").encode("utf-8", "backslashreplace")
        full.update(data)
        if not typed:
            plain.update(data)
    return plain.hexdigest(), full.hexdigest()


def tree_digest(root) -> str:
    """The digest of an expression tree with its type annotations (see tree_digests)."""
    return tree_digests(root)[1]


# What every pinned table held when it was built: id -> (the table, its rows, its columns).
# The rows are kept as a tuple of the row tuples themselves, so that a later
# change of the table in place (a row dropped, replaced or reordered) shows.
_PINNED_ROWS: dict = {}
# Tables found to hold exactly a pinned table's data: id -> (the table, its rows, its columns), kept the same way.
_VERIFIED_TABLES: dict = {}


def _snapshot_pinned() -> None:
    _PINNED_ROWS.clear()
    for tables in EXECUTOR_TABLES or ():
        for table in tables.values():
            _PINNED_ROWS[id(table)] = (table, tuple(table.rows), tuple(table.columns))


def _same_table(table, pinned) -> bool:
    """True when `table` holds exactly the data of the pinned table right now: same columns, same rows, same value types.

    Checked on every use: being the pinned object, or having been equal to
    it once, says nothing about what was done to its rows since.
    """
    import operator

    try:
        known = _PINNED_ROWS.get(id(pinned))
        if known is None or known[0] is not pinned:
            return False
        _, pinned_rows, pinned_columns = known
        rows = table.rows
        if type(table).__name__ != "Table" or type(rows) is not list or table.column_range is not None:
            return False
        if tuple(table.columns) != pinned_columns or len(rows) != len(pinned_rows):
            return False
        reader = table.reader
        if type(reader).__name__ != "RowReader" or reader.columns != {name: index for index, name in enumerate(pinned_columns)}:
            return False
        if table is pinned:
            return all(map(operator.is_, rows, pinned_rows))
        seen = _VERIFIED_TABLES.get(id(table))
        if seen is not None and seen[0] is table:
            # Compared in full before: the very same row tuples must still be there.
            return len(rows) == len(seen[1]) and all(map(operator.is_, rows, seen[1]))
        if rows != list(pinned_rows):
            return False
        if any(type(row) is not tuple or list(map(type, row)) != list(map(type, other)) for row, other in zip(rows, pinned_rows)):
            return False
    except Exception:
        return False
    _VERIFIED_TABLES[id(table)] = (table, tuple(rows), pinned_columns)
    return True


def memo_key(scope: str, digest: str, executor_args: tuple, executor_kwargs: dict) -> str | None:
    """The memo key of one `execute` call, or None when its tables are not the pinned ones.

    The executor is built as `PythonExecutor(tables=...)` with what
    `ensure_tables` made of the tables the test passed. The recorded result
    stands for a query only when those are the same tables, by name and by
    content, as at the pinned commit; their names are part of the key.
    """
    if EXECUTOR_TABLES is None or executor_args or list(executor_kwargs) != ["tables"]:
        return None
    pinned = EXECUTOR_TABLES[0 if scope == "tpch" else 1]
    tables = executor_kwargs["tables"]
    mapping = getattr(tables, "mapping", None)
    if type(tables).__name__ != "Tables" or type(mapping) is not dict:
        return None
    for name, table in mapping.items():
        if type(name) is not str or name not in pinned or not _same_table(table, pinned[name]):
            return None
    return scope + ":" + ",".join(sorted(mapping)) + ":" + digest


def install_executor_memo() -> None:
    """Skip planning and executing a TPC query that is unchanged since the pinned commit.

    `sqlglot.executor.execute` prepares the tables, optimizes the query, plans
    it and runs the plan. The result depends only on the optimized query, on
    the tables, and on everything else `execute` runs: the code that prepares
    the tables, the planner and the executor. This is installed only for a
    patch that changes none of that code and nothing that code reads
    (maps/execphase.json, maps/execnames.json), so a query whose optimized form
    is exactly the pinned one, run on exactly the pinned tables, must give the
    pinned result, which is returned without running it. Any other query is
    planned and executed as usual.
    """
    import pickle

    import sqlglot.executor as executor

    real_plan = executor.Plan
    real_executor = executor.PythonExecutor
    if getattr(real_plan, "_br_memo", False):
        return

    def plan(expression):
        scope = MEMO_SCOPE
        if scope is None:
            return real_plan(expression)
        if MEMO_PHASE is not None:
            MEMO_PHASE("X")
        try:
            return _Planned(expression, *tree_digests(expression))
        except Exception:
            return real_plan(expression)

    plan._br_memo = True

    class Executor:
        def __init__(self, *args, **kwargs) -> None:
            self._args = args
            self._kwargs = kwargs

        def execute(self, planned):
            try:
                if not isinstance(planned, _Planned):
                    return real_executor(*self._args, **self._kwargs).execute(planned)
                scope = MEMO_SCOPE
                key = None if scope is None else memo_key(scope, planned.digest, self._args, self._kwargs)
                if (
                    key is not None
                    and MEMO_RECORD is None
                    and EXECUTOR_MEMO is not None
                    and key in EXECUTOR_MEMO
                    and _query_untouched(key, planned.typed)
                ):
                    return pickle.loads(EXECUTOR_MEMO[key])
                result = real_executor(*self._args, **self._kwargs).execute(real_plan(planned.expression))
                if MEMO_RECORD is not None and key is not None:
                    name = os.path.join(MEMO_RECORD, f"{os.getpid()}-{len(os.listdir(MEMO_RECORD))}")
                    with open(name, "wb") as handle:
                        pickle.dump((key, planned.typed, pickle.dumps(result, protocol=4)), handle)
                return result
            finally:
                if MEMO_PHASE is not None and MEMO_SCOPE is not None:
                    MEMO_PHASE("L")

        def __getattr__(self, name):
            return getattr(real_executor(*self._args, **self._kwargs), name)

    executor.Plan = plan
    executor.PythonExecutor = Executor
    if MEMO_PHASE is not None:
        # Recording only. The executor also runs on tables that `execute`
        # prepares before the query is optimized, so that code decides the
        # result too: everything `execute` does counts, except optimizing.
        real_tables = executor.ensure_tables
        real_optimize = executor.optimize

        def ensure_tables(*args, **kwargs):
            if MEMO_SCOPE is not None:
                MEMO_PHASE("X")
            return real_tables(*args, **kwargs)

        def optimize(*args, **kwargs):
            if MEMO_SCOPE is not None:
                MEMO_PHASE("L")
            return real_optimize(*args, **kwargs)

        executor.ensure_tables = ensure_tables
        executor.optimize = optimize


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


class _InlineFuture:
    """The result of work handed to an _InlinePool; the work runs when it is first asked for."""

    def __init__(self, function, args, kwargs) -> None:
        self._call = (function, args, kwargs)
        self._outcome = None
        self._callbacks: list = []

    def _run(self):
        if self._outcome is None:
            import pickle

            try:
                # A pool pickles the call on its way to a worker and the result
                # on its way back; both can fail, and the copy is what the test sees.
                function, args, kwargs = pickle.loads(pickle.dumps(self._call, pickle.HIGHEST_PROTOCOL))
                value = function(*args, **kwargs)
                self._outcome = (True, pickle.loads(pickle.dumps(value, pickle.HIGHEST_PROTOCOL)))
            except BaseException as error:  # noqa: BLE001 - a pool reports any of them through the future
                self._outcome = (False, error)
            self._call = None
            callbacks, self._callbacks = self._callbacks, []
            for callback in callbacks:
                callback(self)
        return self._outcome

    def result(self, timeout=None):
        done, value = self._run()
        if done:
            return value
        raise value

    def exception(self, timeout=None):
        done, value = self._run()
        return None if done else value

    def add_done_callback(self, callback) -> None:
        if self._outcome is None:
            self._callbacks.append(callback)
        else:
            callback(self)

    def done(self) -> bool:
        self._run()
        return True

    def cancel(self) -> bool:
        return False

    def cancelled(self) -> bool:
        return False

    def running(self) -> bool:
        return False


class _InlinePool:
    """Stands in for a ProcessPoolExecutor: the work is done in this process.

    The tests use pools only to spread independent calls over cores. Here CPU
    time is what counts, so the calls run one after the other, each when its
    result is asked for; a test that stops at its first failure then never
    pays for the calls it did not get to. Arguments and results are pickled
    and unpickled as a pool would do.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs) -> None:
        if initializer is not None:
            initializer(*initargs)

    def submit(self, function, /, *args, **kwargs):
        return _InlineFuture(function, args, kwargs)

    def map(self, function, *iterables, timeout=None, chunksize=1):
        futures = [self.submit(function, *args) for args in zip(*iterables)]
        return (future.result() for future in futures)

    def shutdown(self, wait=True, *, cancel_futures=False) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def _inline_wait(futures, timeout=None, return_when=None):
    import concurrent.futures

    futures = set(futures)
    for future in futures:
        future.done()
    return concurrent.futures._base.DoneAndNotDoneFutures(futures, set())


def _inline_as_completed(futures, timeout=None):
    # Any order is a possible order of completion.
    return iter(list(futures))


def _patch_test_modules() -> None:
    """Adapt the test modules' helpers to this process.

    Process pools: the tests ask for spawned workers, which would import the
    unpatched tree in hot mode and cost a fresh interpreter each either way.
    Their work is done in this (patched) process instead, see _InlinePool.

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
            module.ProcessPoolExecutor = _InlinePool if INLINE_POOLS else Pool
            if INLINE_POOLS:
                if getattr(module, "wait", None) is concurrent.futures.wait:
                    module.wait = _inline_wait
                if getattr(module, "as_completed", None) is concurrent.futures.as_completed:
                    module.as_completed = _inline_as_completed
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
        if MEMO_SCOPE is not None and MEMO_AFTER_IMPORT:
            # After a clean import the executor exists only once the test module is imported.
            try:
                install_executor_memo()
            except Exception:
                pass
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
            # A class teardown error: pytest reports it against the last test of
            # the class. Which one that is, the coordinator knows.
            emit(f"C {cases[-1]._br_index}\n")


def child_main(job, write_fd: int, root: str, function_index) -> None:
    """Runs in the forked child and never returns."""
    global _pinned_fixtures, _changed_fixtures, MEMO_AFTER_IMPORT, MEMO_BLOCK
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

        if GC_THRESHOLD:
            import gc

            gc.set_threshold(*GC_THRESHOLD)
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
        MEMO_BLOCK = job.get("memo_block")
        if job["mode"] == "clean":
            _purge(root)
            sys.path.insert(0, overlay)
            os.chdir(overlay)
            if changes:
                _keep_resolved_paths_in(overlay, root)
            MEMO_AFTER_IMPORT = bool(job.get("executor_memo") and EXECUTOR_MEMO and EXECUTOR_TABLES is not None)
        else:
            lazy = list(job.get("lazy") or ())
            swaps = []
            for path, pairs in job["swaps"]:
                if _module_name(path, root) in sys.modules:
                    swaps.append((path, pairs))
                else:
                    # Not imported after all: there are no functions to swap, the new source is imported instead.
                    lazy.append((path, changes[os.path.relpath(path, root).replace(os.sep, "/")]))
            if lazy:
                _serve_patched(lazy, root)
            _apply_swaps(swaps, function_index)
            # Whatever opens a patched file by its path in the pinned tree gets the
            # patched text: fixtures, and source files that a test reads or runs as data.
            present = {
                os.path.join(root, path): os.path.join(overlay, path)
                for path, text in changes.items()
                if text is not None
            }
            if present:
                _install_redirects(present)
            if job.get("executor_tables") and EXECUTOR_TABLES is not None:
                _use_executor_tables()
                if job.get("executor_memo") and EXECUTOR_MEMO:
                    install_executor_memo()
        if job.get("duckdb_probe"):
            _install_duck_probe((overlay if changes else "", root), set(changes))
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
