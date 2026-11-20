"""Offline tracer used to build the dependency map (never shipped in the image's
runtime path). Auto-imported by every Python process whose PYTHONPATH holds this
directory. When BR_TRACE_DIR is set, it records, for the whole life of the
process:

  L <file> <code firstlineno> <line>   every (code object, line) executed in sqlglot's tree
  O <path>                             every file opened under the sqlglot checkout
  D <path>                             every directory listed under the sqlglot checkout

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
    _path = os.path.join(_DIR, f"{os.getpid()}.txt")

    def _on_line(code, line):
        filename = code.co_filename
        if filename.startswith(_ROOT):
            _seen.append((filename, code.co_firstlineno, line))
        return _DISABLE

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

    def _dump():
        global _seen
        seen, _seen = _seen, []
        files = sorted(_files)
        _files.clear()
        if not seen and not files:
            return
        with open(_path, "a", encoding="utf-8") as handle:
            for filename, first, line in seen:
                handle.write(f"L {filename} {first} {line}\n")
            for entry in files:
                handle.write(entry + "\n")

    _mon.use_tool_id(_TOOL, "blast-radius-trace")
    _mon.register_callback(_TOOL, _mon.events.LINE, _on_line)
    _mon.set_events(_TOOL, _mon.events.LINE)
    sys.addaudithook(_audit)
    atexit.register(_dump)
    try:
        # multiprocessing children leave through os._exit and skip atexit.
        from multiprocessing import util as _util

        _util.Finalize(None, _dump, exitpriority=-100)
    except Exception:
        pass
