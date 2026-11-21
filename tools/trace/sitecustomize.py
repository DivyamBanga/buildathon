"""Offline tracer used to build the dependency map (never part of the image).

Auto-imported by every Python process whose PYTHONPATH holds this directory.
When BR_TRACE_DIR is set, it records, for the whole life of the process:

  L <file> <code firstlineno> <line>   every (code object, line) executed in sqlglot's tree
  O <path>                             every file opened under the sqlglot checkout
  D <path>                             every directory listed under the sqlglot checkout
  C <file> <qualname>                  every class of sqlglot's tree that was instantiated,
                                       and each of its base classes (BR_TRACE_CLASSES=1)

Each process appends to its own file, so the union of the files in the
directory is everything one isolated test run touched, child processes included.
"""
import os
import sys

_DIR = os.environ.get("BR_TRACE_DIR")
_ROOT = os.environ.get("BR_TRACE_ROOT", "/sqlglot/")

if _DIR:
    import atexit

    _mon = sys.monitoring
    _TOOL = _mon.COVERAGE_ID
    _DISABLE = _mon.DISABLE
    _seen = []
    _files = set()
    _classes = set()
    _getframe = sys._getframe

    def _on_line(code, line):
        filename = code.co_filename
        if filename.startswith(_ROOT):
            _seen.append((filename, code.co_firstlineno, line))
        return _DISABLE

    _CLASSES = bool(os.environ.get("BR_TRACE_CLASSES"))

    def _on_start(code, offset):
        # Function starts are always monitored, even when nothing is recorded
        # here: with line events alone, CPython 3.13 does not report the first
        # line of a function, so a one-line lambda would never be seen running.
        # Only constructors stay monitored; `self` tells which class was built.
        if not _CLASSES or code.co_name != "__init__" or not code.co_argcount:
            return _DISABLE
        try:
            _classes.add(type(_getframe(1).f_locals[code.co_varnames[0]]))
        except Exception:
            pass
        return None

    def _on_call(code, offset, target, argument):
        # A class called directly (covers classes with no Python-level __init__).
        if isinstance(target, type):
            _classes.add(target)
        return None

    def _audit(event, args):
        if event == "open":
            path = args[0]
            if isinstance(path, bytes):
                path = os.fsdecode(path)
            if isinstance(path, str):
                if not path.startswith("/"):
                    path = os.path.abspath(path)
                if path.startswith(_ROOT) and "__pycache__" not in path:
                    _files.add("O " + path)
        elif event in ("os.listdir", "os.scandir", "glob.glob", "glob.glob/2"):
            path = args[0]
            if isinstance(path, bytes):
                path = os.fsdecode(path)
            if isinstance(path, str):
                if not path.startswith("/"):
                    path = os.path.abspath(path)
                if path.startswith(_ROOT) and "__pycache__" not in path:
                    _files.add("D " + path)

    _kind = "L"

    def _mark():
        """From here on, lines are recorded afresh and written as `P` rows.

        Used when tracing one fixture case: everything that runs after the case
        is handed to the test belongs to that case.
        """
        global _kind
        if _kind == "L":
            _dump()
            _kind = "P"
            _mon.restart_events()

    def _phase(kind):
        """Write what was recorded so far and record afresh under another row letter."""
        global _kind
        if kind != _kind:
            _dump()
            _kind = kind
            _mon.restart_events()

    def _dump():
        global _seen
        seen, _seen = _seen, []
        rows = sorted(_files)
        _files.clear()
        classes = list(_classes)
        _classes.clear()
        for cls in classes:
            try:
                bases = cls.__mro__
            except Exception:
                continue
            for base in bases:
                module = sys.modules.get(getattr(base, "__module__", None) or "")
                origin = getattr(module, "__file__", None) or ""
                if origin.startswith(_ROOT):
                    rows.append(f"C {origin} {base.__qualname__}")
        if not seen and not rows:
            return
        with open(os.path.join(_DIR, f"{os.getpid()}.txt"), "a", encoding="utf-8") as handle:
            for filename, first, line in seen:
                handle.write(f"{_kind} {filename} {first} {line}\n")
            for entry in rows:
                handle.write(entry + "\n")

    def _preload():
        """Import the whole sqlglot package, so that nothing is imported lazily later on."""
        import importlib

        base = os.path.join(_ROOT, "sqlglot")
        for directory, subdirs, files in os.walk(base):
            subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
            for name in sorted(files):
                if name.endswith(".py") and name != "__main__.py":
                    parts = os.path.relpath(os.path.join(directory, name), _ROOT)[:-3].split(os.sep)
                    if parts[-1] == "__init__":
                        parts.pop()
                    try:
                        importlib.import_module(".".join(parts))
                    except BaseException:
                        pass

    def _enter_test_phase():
        """Import sqlglot in full (`L` rows), then write every later row as `T`.

        `T` rows are then what a test makes run beyond sqlglot importing itself:
        the import of its own test module, its setup and the test. Processes the
        test starts inherit BR_TRACE_SPLIT and do the same.
        """
        _preload()
        _phase("T")

    _events = _mon.events.LINE | _mon.events.PY_START
    _mon.use_tool_id(_TOOL, "blast-radius-trace")
    _mon.register_callback(_TOOL, _mon.events.LINE, _on_line)
    _mon.register_callback(_TOOL, _mon.events.PY_START, _on_start)
    if _CLASSES:
        _mon.register_callback(_TOOL, _mon.events.CALL, _on_call)
        _events |= _mon.events.CALL
    _mon.set_events(_TOOL, _events)
    sys.addaudithook(_audit)
    atexit.register(_dump)
    try:
        # multiprocessing children leave through os._exit and skip atexit; a
        # forked child also forgets its parent's finalizers, so re-register there.
        from multiprocessing import util as _util

        def _arm(_=None):
            _util.Finalize(None, _dump, exitpriority=-100)

        _arm()
        _util.register_after_fork(_dump, _arm)
    except Exception:
        pass
    if os.environ.get("BR_TRACE_SPLIT"):
        _enter_test_phase()
