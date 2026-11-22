"""Checks for the rows kept at image build time (selector/prebuild.py, runner._prebuilt_tables).

    python tools/test_prebuild.py        (in the lab image, PYTHONPATH=/sqlglot:/repo)

The tables made from the kept rows must be what the pinned class setup builds: same tables in the same order,
same columns, same rows, same value types. A file whose digest does not match what is found is not used.
"""
import os
import pickle
import sys
import tempfile
import time

sys.path.insert(0, "/repo")
from selector import runner  # noqa: E402

os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up("/sqlglot", ["tests/test_executor.py"])
checks = 0


def check(condition, what):
    global checks
    assert condition, what
    checks += 1


started = time.process_time()
runner.build_executor_tables()
parse_seconds = time.process_time() - started
parsed = runner.EXECUTOR_TABLES
uses = runner._SETUP_USES
check(parsed is not None and len(parsed) == 2, "two groups of tables")

with tempfile.TemporaryDirectory() as directory:
    path = os.path.join(directory, "exectables.bin")
    size = runner.save_executor_rows(path)
    check(size == os.path.getsize(path) and size > 1_000_000, "file written")

    runner.EXECUTOR_TABLES = None
    started = time.process_time()
    runner.build_executor_tables(path)
    load_seconds = time.process_time() - started
    kept = runner.EXECUTOR_TABLES
    check(kept is not None and kept is not parsed, "tables made from the kept rows")
    check(runner._SETUP_USES == uses, "the setup's inputs are noted as before")
    for group_parsed, group_kept in zip(parsed, kept):
        check(list(group_parsed) == list(group_kept), "same tables in the same order")
        for name, table in group_parsed.items():
            other = group_kept[name]
            check(type(other) is type(table), f"{name}: same class")
            check(other.columns == table.columns and type(other.columns) is tuple, f"{name}: columns")
            check(other.column_range == table.column_range, f"{name}: column range")
            check(other.reader.columns == table.reader.columns, f"{name}: reader")
            check(type(other.rows) is list and len(other.rows) == len(table.rows), f"{name}: number of rows")
            check(other.rows == table.rows, f"{name}: rows")
            check(
                all(type(a) is tuple and [type(x) for x in a] == [type(y) for y in b] for a, b in zip(other.rows, table.rows)),
                f"{name}: value types",
            )
    check(len(runner._PINNED_ROWS) == sum(len(group) for group in kept), "snapshot of the kept tables")
    for group in kept:
        for table in group.values():
            check(runner._same_table(table, table), "a kept table is a pinned table")

    # Another digest: the file is not used and the setup parses.
    with open(path, "rb") as handle:
        data = pickle.load(handle)
    data["digest"] = "0" * 40
    with open(path, "wb") as handle:
        pickle.dump(data, handle, protocol=pickle.HIGHEST_PROTOCOL)
    check(runner._prebuilt_tables(path, sys.modules["tests.test_executor"]) is None, "stale file refused")
    # A file that is not there, or not a pickle.
    check(runner._prebuilt_tables(os.path.join(directory, "missing.bin"), sys.modules["tests.test_executor"]) is None, "missing file")
    with open(path, "wb") as handle:
        handle.write(b"not a pickle")
    check(runner._prebuilt_tables(path, sys.modules["tests.test_executor"]) is None, "broken file")
    runner.EXECUTOR_TABLES = None
    runner.build_executor_tables(path)
    check(runner.EXECUTOR_TABLES is not None and list(runner.EXECUTOR_TABLES[1]) == list(parsed[1]), "falls back to parsing")

print(f"{checks} checks passed; parsing {parse_seconds:.2f} CPU s, from the kept rows {load_seconds:.2f} CPU s, file {size} bytes")
