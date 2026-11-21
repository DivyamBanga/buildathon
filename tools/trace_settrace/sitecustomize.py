"""Reference tracer for cross-checking tools/trace/sitecustomize.py.

Uses the classic sys.settrace line tracer (slow, but the long-standing
mechanism) and writes the same `L <file> <code firstlineno> <line>` rows.
"""
import os
import sys
import threading

_DIR = os.environ.get("BR_TRACE_DIR")
_ROOT = os.environ.get("BR_TRACE_ROOT", "/sqlglot/")

if _DIR:
    import atexit

    _seen = set()

    def _local(frame, event, arg):
        if event == "line":
            code = frame.f_code
            _seen.add((code.co_filename, code.co_firstlineno, frame.f_lineno))
        return _local

    def _global(frame, event, arg):
        code = frame.f_code
        if code.co_filename.startswith(_ROOT):
            return _local
        return None

    def _dump():
        rows = sorted(_seen)
        _seen.clear()
        if rows:
            with open(os.path.join(_DIR, f"{os.getpid()}.txt"), "a", encoding="utf-8") as handle:
                for filename, first, line in rows:
                    handle.write(f"L {filename} {first} {line}\n")

    sys.settrace(_global)
    threading.settrace(_global)
    atexit.register(_dump)
    try:
        from multiprocessing import util as _util

        def _arm(_=None):
            _util.Finalize(None, _dump, exitpriority=-100)

        _arm()
        _util.register_after_fork(_dump, _arm)
    except Exception:
        pass
