"""Trace the whole suite in ONE process, in collection order, test by test.

    python trace_suite.py /data/tests.json /out/suite        (inside the lab image, cwd /sqlglot)

tools/trace_all.py runs every test alone in a fresh process. The organisers
label a commit by running the whole suite, where a test also runs with whatever
the tests before it left behind: caches already filled, modules already
imported, registries emptied by another test's tearDown. This records, for
every test, the (code object, line) pairs it executes in that setting, so that
the lines a test reaches only in the suite can be told apart (build_suite.py).

Writes /out/suite/<number>.txt, one "path first line" row per pair, and
results.json with pytest's verdicts. Worker processes of the tests' own pools
are not traced: they outlive single tests, and the selector runs that work in
the test's own process.
"""
import json
import os
import sys

import pytest

# The tests start process pools with the "spawn" method, and a spawned worker
# imports this file again: everything below must only run in the main process.
if __name__ == "__main__":
    tests = json.load(open(sys.argv[1]))["tests"]
    out = sys.argv[2]
    os.makedirs(out, exist_ok=True)
    number = {test["id"]: test["number"] for test in tests}
    ROOT = "/sqlglot/"

    _mon = sys.monitoring
    _TOOL = _mon.COVERAGE_ID
    _DISABLE = _mon.DISABLE
    seen = []


    def _on_line(code, line):
        filename = code.co_filename
        if filename.startswith(ROOT):
            seen.append((filename, code.co_firstlineno, line))
        return _DISABLE


    def _on_start(code, offset):
        # As in tools/trace/sitecustomize.py: with line events alone CPython 3.13
        # does not report a function's first line.
        return _DISABLE


    class Plugin:
        def __init__(self):
            self.outcomes = {}
            self.order = []

        def pytest_runtest_logstart(self, nodeid, location):
            del seen[:]
            _mon.restart_events()

        def pytest_runtest_logreport(self, report):
            if report.failed:
                self.outcomes[report.nodeid] = "failed"

        def pytest_runtest_logfinish(self, nodeid, location):
            rows = sorted(set(seen))
            del seen[:]
            self.order.append(nodeid)
            index = number.get(nodeid)
            if index is None:
                return
            with open(os.path.join(out, f"{index}.txt"), "w", encoding="utf-8") as handle:
                for filename, first, line in rows:
                    handle.write(f"{filename[len(ROOT):]} {first} {line}\n")


    plugin = Plugin()
    _mon.use_tool_id(_TOOL, "blast-radius-suite-trace")
    _mon.register_callback(_TOOL, _mon.events.LINE, _on_line)
    _mon.register_callback(_TOOL, _mon.events.PY_START, _on_start)
    _mon.set_events(_TOOL, _mon.events.LINE | _mon.events.PY_START)
    os.environ["SKIP_INTEGRATION"] = "0"
    code = pytest.main(["tests", "-q", "-p", "no:cacheprovider"], plugins=[plugin])
    _mon.set_events(_TOOL, 0)
    with open(os.path.join(out, "results.json"), "w") as handle:
        json.dump({"exit": int(code), "failed": sorted(plugin.outcomes), "order": plugin.order}, handle)
    ran = [nodeid for nodeid in plugin.order if nodeid in number]
    in_order = [number[nodeid] for nodeid in ran] == sorted(number[nodeid] for nodeid in ran)
    print(f"exit {int(code)}, {len(ran)} of {len(tests)} listed tests ran, {len(plugin.outcomes)} failed, "
          f"same order as tests.json: {in_order}")
