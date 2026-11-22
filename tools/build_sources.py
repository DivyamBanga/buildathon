"""Record which tests read a Python source file of the checkout as a file.

    python build_sources.py /data/tests.json /out/trace /out/maps

Run inside an image whose checkout is compiled (the submission image), with
tools/trace on hand. Importing a module does not open its source there (the
compiled file is read), and pytest opens only the test file it runs. Any other
source a test opens is data to it: it reads it, parses it, or runs it with
runpy. Such a test depends on the file's text whichever of its lines are ever
executed, so it is a candidate for every change to that file.

  sources.json   source path (outside tests/) -> numbers of the tests that open it

The per-test traces behind cov.bin (rows "O <path>") name the suspects: they
were made on an uncompiled tree, where the first import of a module also opens
its source. Each suspect is run again here, alone, on the compiled tree.
"""
import json
import os
import shutil
import subprocess
import sys

tests_path, trace_dir, out_dir = sys.argv[1:4]
ROOT = "/sqlglot/"
HERE = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(tests_path))["tests"]


def sources_opened(rows):
    found = set()
    for row in rows:
        row = row.rstrip("\n")
        if row.startswith("O ") and row.endswith(".py"):
            path = os.path.normpath(row[2:])
            if path.startswith(ROOT) and not path[len(ROOT):].startswith("tests/"):
                found.add(path[len(ROOT):])
    return found


suspects = []
for test in tests:
    with open(os.path.join(trace_dir, f"{test['number']}.txt"), encoding="utf-8") as handle:
        if sources_opened(handle):
            suspects.append(test)
print(len(suspects), "tests opened a source file in the first trace", flush=True)

readers = {}
for test in suspects:
    work = f"/tmp/sources-{test['number']}"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    env = dict(os.environ, BR_TRACE_DIR=work, PYTHONPATH="/sqlglot:" + os.path.join(HERE, "trace"),
               SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, "-m", "pytest", test["id"], "-q", "-p", "no:cacheprovider", "-x"],
                   cwd="/sqlglot", env=env, capture_output=True, text=True)
    for name in os.listdir(work):
        with open(os.path.join(work, name), encoding="utf-8") as handle:
            for path in sources_opened(handle):
                readers.setdefault(path, set()).add(test["number"])
    shutil.rmtree(work, ignore_errors=True)

# Tests that read the code objects of a module's functions without running them (`__code__`: the
# instructions, names and constants). A change inside such a function is to them what a change of
# the text is to a test that parses the file, whichever of its lines they ever execute. Read at the
# pinned commit (the only reads of `__code__` under tests/): tests/test_typing.py compares the
# annotators of every sqlglot.typing module with those of the module it inherits from. It opens the
# modules that inherit (found above) and never the root of them all, whose annotators it compares
# all the same.
CODE_READERS = {
    "sqlglot/typing/__init__.py": ("tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata",),
}
for path, ids in CODE_READERS.items():
    for test in tests:
        if test["id"] in ids:
            readers.setdefault(path, set()).add(test["number"])

os.makedirs(out_dir, exist_ok=True)
json.dump({path: sorted(numbers) for path, numbers in sorted(readers.items())},
          open(os.path.join(out_dir, "sources.json"), "w"), separators=(",", ":"))
for path, numbers in sorted(readers.items()):
    print(path, sorted(tests[n]["id"].split("::")[-1] for n in numbers))
