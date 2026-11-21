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
  S <path>            an entry of sys.path under the checkout, as it is when the process ends

Directories are recorded under the checkout root and the root itself.
"""
import os
import sys

_DIR = os.environ.get("BR_TRACE_DIR")
_ROOT = os.environ.get("BR_TRACE_ROOT", "/sqlglot")

if _DIR:
    import atexit

    _names = set()
    _rows = set()

    class _Logger:
        @staticmethod
        def find_spec(name, path=None, target=None):
            _names.add(name)
            return None

    sys.meta_path.insert(0, _Logger)

    def _audit(event, args):
        if event not in ("os.listdir", "os.scandir", "glob.glob", "glob.glob/2"):
            return
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
        frame = sys._getframe(1)
        kind = "D"
        where = ""
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
            break
        if kind == "D":
            # The innermost frame of the checkout itself that is on the stack.
            while frame is not None and not frame.f_code.co_filename.startswith(_ROOT + "/"):
                frame = frame.f_back
            where = f"{frame.f_code.co_filename}:{frame.f_lineno}" if frame is not None else "-"
            _rows.add(f"W {path} {where}")
        _rows.add(f"{kind} {path}")

    def _dump():
        for entry in sys.path:
            entry = os.path.normpath(os.path.abspath(entry or os.getcwd()))
            if entry == _ROOT or entry.startswith(_ROOT + "/"):
                _rows.add(f"S {entry}")
        rows = sorted(_rows) + [f"N {name}" for name in sorted(_names)]
        if not rows:
            return
        with open(os.path.join(_DIR, f"{os.getpid()}.txt"), "a", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n")

    sys.addaudithook(_audit)
    atexit.register(_dump)
    try:
        from multiprocessing import util as _util

        def _arm(_=None):
            _util.Finalize(None, _dump, exitpriority=-100)

        _arm()
        _util.register_after_fork(_dump, _arm)
    except Exception:
        pass
