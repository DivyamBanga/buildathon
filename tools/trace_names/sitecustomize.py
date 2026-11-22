"""Offline tracer for the map of directory listings and import attempts (never part of the image).

Auto-imported by every Python process whose PYTHONPATH holds this directory.
When BR_TRACE_DIR is set it records, for the whole life of the process:

  N <module name>     every module name the import system was asked for that was not
                      yet in sys.modules (found or not): a new file can change the
                      answer only for a name that is asked for
  I <path>            a directory listed by the import system itself (importlib's
                      FileFinder): only the names it is asked for are looked up in it
  P <path>            a directory listed by pytest while it collects
  D <path>            a directory listed by anything else (os.listdir, os.scandir, glob,
                      pkgutil, importlib.metadata, a test's own code): every entry counts
  W <path> <where>    the same as D, with the file and line of the caller, as evidence
  V <path> <file>:<function>
                      for a D row: the first frame outside os, glob and pathlib that asked
  S <path>            an entry of sys.path under the checkout, as it is when the process ends
  Rows are written as they happen, one file per process id; a name that was imported before
  this file ran (the interpreter's start-up) is an N row too.

Directories are recorded under the checkout root and the root itself.
"""
import os
import sys

_DIR = os.environ.get("BR_TRACE_DIR")
_ROOT = os.environ.get("BR_TRACE_ROOT", "/sqlglot")

if _DIR:
    import atexit

    _rows = set()
    _pid = [None, None]

    def _emit(row):
        """Append a row to this process's own file at once (a process that is killed, a pool
        worker that is terminated and a forked child all leave what they saw)."""
        if row in _rows:
            return
        _rows.add(row)
        pid = os.getpid()
        if _pid[0] != pid:
            _pid[0] = pid
            _pid[1] = os.open(os.path.join(_DIR, f"{pid}.txt"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.write(_pid[1], (row + chr(10)).encode("utf-8", "replace"))

    # What was imported before this file ran (the interpreter's own start-up, `site`, the
    # codecs): those names were asked of the import system, with the entries of PYTHONPATH in
    # sys.path already, and cannot be seen by the finder below.
    for _name in sorted(sys.modules):
        _emit(f"N {_name}")

    class _Logger:
        @staticmethod
        def find_spec(name, path=None, target=None):
            _emit(f"N {name}")
            return None

    sys.meta_path.insert(0, _Logger)

    def _audit(event, args):
        if event not in ("os.listdir", "os.scandir", "glob.glob", "glob.glob/2"):
            return
        try:
            path = args[0]
            if isinstance(path, bytes):
                path = os.fsdecode(path)
            if not isinstance(path, str):
                return
            if not path.startswith("/"):
                path = os.path.abspath(path)
            path = os.path.normpath(path)
            if not (path == _ROOT or path.startswith(_ROOT + "/")) or "__pycache__" in path:
                return
        except Exception:
            return
        kind = "D"
        where = "-"
        first = "-"
        try:
            frame = sys._getframe(1)
            while frame is not None:
                name = frame.f_code.co_filename
                base = os.path.basename(name)
                if base in ("os.py", "glob.py", "genericpath.py", "posixpath.py") or name.endswith("/pathlib/_abc.py") or name.endswith("/pathlib/_local.py") or name.endswith("/pathlib.py"):
                    frame = frame.f_back
                    continue
                if "importlib._bootstrap" in name:
                    kind = "I"
                elif "/_pytest/" in name or "/pluggy/" in name:
                    kind = "P"
                first = f"{name}:{frame.f_code.co_name}"
                break
            if kind == "D":
                # The innermost frame of the checkout itself that is on the stack.
                while frame is not None and not frame.f_code.co_filename.startswith(_ROOT + "/"):
                    frame = frame.f_back
                where = f"{frame.f_code.co_filename}:{frame.f_lineno}" if frame is not None else "-"
        except Exception:
            kind = "D"
        if kind == "D":
            _emit(f"W {path} {where}")
            _emit(f"V {path} {first}")
        _emit(f"{kind} {path}")

    def _dump():
        for entry in sys.path:
            entry = os.path.normpath(os.path.abspath(entry or os.getcwd()))
            if entry == _ROOT or entry.startswith(_ROOT + "/"):
                _emit(f"S {entry}")

    sys.addaudithook(_audit)
    atexit.register(_dump)
