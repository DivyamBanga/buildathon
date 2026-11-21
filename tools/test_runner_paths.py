"""Unit checks for how a child finds patched files: redirects, resolved paths, modules imported on demand.

    PYTHONPATH=/repo python tools/test_runner_paths.py

Each part runs in its own forked process, because the code under test replaces
builtins.open, io.open_code, os.path.realpath and adds an import finder.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from selector import runner  # noqa: E402

checks = 0


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        raise SystemExit("FAILED: " + message)


def in_child(function):
    """Run `function` in a fork; it returns how many checks it made."""
    global checks
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        before = checks
        try:
            function()
            os.write(write_fd, str(checks - before).encode())
            os._exit(0)
        except SystemExit as error:
            os.write(write_fd, str(error).encode())
            os._exit(1)
        except BaseException as error:  # noqa: BLE001
            os.write(write_fd, f"FAILED: {function.__name__} raised {error!r}".encode())
            os._exit(1)
    os.close(write_fd)
    data = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    if status:
        raise SystemExit(data or f"FAILED: {function.__name__}")
    checks += int(data)


base = os.path.realpath(tempfile.mkdtemp())
root = os.path.join(base, "pinned")
overlay = os.path.join(base, "overlay")
os.makedirs(os.path.join(root, "pkg", "sub"))
os.makedirs(os.path.join(root, "tests"))
os.makedirs(os.path.join(root, "scripts"))
for path, text in {
    "pkg/__init__.py": "",
    "pkg/sub/__init__.py": "",
    "pkg/sub/leaf.py": "VALUE = 'pinned'\n\ndef get():\n    return VALUE\n",
    "pkg/other.py": "from pkg.sub import leaf\n\ndef read():\n    return leaf.get()\n",
    "tests/test_x.py": "pass\n",
    "scripts/setup.py": "RESULT = 'pinned script'\n",
    "data.txt": "pinned data\n",
}.items():
    with open(os.path.join(root, path), "w") as handle:
        handle.write(text)
changes = {"scripts/setup.py": "RESULT = 'patched script'\n", "data.txt": "patched data\n"}
runner.build_overlay(root, overlay, changes)


def redirects():
    import io
    import pathlib
    import runpy

    runner._install_redirects(
        {os.path.join(root, path): os.path.join(overlay, path) for path in changes}
    )
    check(open(os.path.join(root, "data.txt")).read() == "patched data\n", "open() by the pinned path gets the patched text")
    check(pathlib.Path(root, "data.txt").read_text() == "patched data\n", "Path.read_text does too")
    check(io.open(os.path.join(root, "data.txt")).read() == "patched data\n", "and io.open")
    with io.open_code(os.path.join(root, "scripts", "setup.py")) as handle:
        check(b"patched script" in handle.read(), "io.open_code is redirected")
    check(runpy.run_path(os.path.join(root, "scripts", "setup.py"))["RESULT"] == "patched script",
          "runpy runs the patched script")
    check(open(os.path.join(root, "pkg", "other.py")).read().startswith("from pkg.sub"), "other files are untouched")
    os.chdir(root)
    check(open("data.txt").read() == "patched data\n", "relative paths are redirected as well")


def resolved_paths():
    import pathlib

    test_file = os.path.join(overlay, "tests", "test_x.py")
    check(os.path.realpath(test_file).startswith(root), "without the fix a resolved path leaves the overlay")
    runner._keep_resolved_paths_in(overlay, root)
    resolved = pathlib.Path(test_file).resolve()
    check(str(resolved) == test_file, f"a resolved path stays in the overlay: {resolved}")
    script = resolved.parents[1] / "scripts" / "setup.py"
    check(script.read_text() == "RESULT = 'patched script'\n", "and leads to the patched file")
    check(os.path.realpath(root) == overlay, "the checkout itself maps to the overlay")
    check(os.path.realpath(base) == base, "paths outside the checkout are left alone")
    check(os.path.realpath(os.fsencode(test_file)) == os.fsencode(test_file), "bytes in, bytes out")
    check(os.path.realpath("/tmp") == os.path.realpath("/tmp"), "unrelated paths still resolve")


def on_demand():
    sys.path.insert(0, root)
    import pkg  # noqa: F401 - the package itself is "already imported"

    leaf_path = os.path.join(root, "pkg", "sub", "leaf.py")
    patched = "VALUE = 'patched'\n\ndef get():\n    return VALUE\n"
    check(runner._module_name(leaf_path, root) == "pkg.sub.leaf", "module name from path")
    check(runner._module_name(os.path.join(root, "pkg", "sub", "__init__.py"), root) == "pkg.sub", "package name from path")
    runner._serve_patched([(leaf_path, patched)], root)
    import pkg.other

    check(pkg.other.read() == "patched", "a module imported later comes from the patched text")
    leaf = sys.modules["pkg.sub.leaf"]
    check(leaf.__file__ == leaf_path, "under its path in the pinned tree")
    check(leaf.get.__code__.co_filename == leaf_path, "also in its code objects")
    check(pkg.sub.leaf is leaf, "and is bound on its package")
    import importlib

    check(importlib.import_module("pkg.sub.leaf") is leaf, "a second import is the same module")
    try:
        runner._serve_patched([(leaf_path, patched)], root)
        raise SystemExit("FAILED: a module that is already imported must be refused")
    except RuntimeError:
        check(True, "")


def on_demand_broken():
    sys.path.insert(0, root)
    leaf_path = os.path.join(root, "pkg", "sub", "leaf.py")
    runner._serve_patched([(leaf_path, "def get(:\n")], root)
    try:
        import pkg.other  # noqa: F401
        raise SystemExit("FAILED: importing a patched module that does not compile must raise")
    except SyntaxError:
        check(True, "")


for part in (redirects, resolved_paths, on_demand, on_demand_broken):
    in_child(part)
print(f"{checks} checks passed")
