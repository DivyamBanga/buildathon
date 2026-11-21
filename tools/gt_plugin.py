"""pytest plugin for labeling: writes the node ids pytest reported as failed.

A test fails when any of its reports (setup, call, teardown, or a subtest)
failed; a file that cannot be collected is recorded as COLLECT::<path>.
"""
import json
import os

_failed = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _failed.add("COLLECT::" + report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["BR_GT_OUT"], "w") as handle:
        json.dump(sorted(_failed), handle)
