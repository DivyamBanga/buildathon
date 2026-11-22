"""Run once while the image is built: keep the rows the executor tests' class setup parses.

    python -m selector.prebuild <output file>

`TestExecutor.setUpClass` reads every TPC-H and TPC-DS table from its CSV
fixture, about 135 000 rows. The coordinator needs those tables in every batch
(runner.build_executor_tables), and parsing them costs it about two CPU
seconds each time. Here the pinned setup itself is run, once, in the image
that will run the batches, and what it parsed is written down together with a
digest of what it was parsed from: the setup's own code, the test module's
`open_file`, the two schemas and the bytes of every fixture file. A batch
takes the rows from that file only while the digest still matches what it
finds (runner._prebuilt_tables); otherwise it parses as before.
"""

from __future__ import annotations

import os
import sys

from . import runner

ROOT = os.environ.get("BR_SQLGLOT", "/sqlglot")


def main() -> None:
    target = sys.argv[1]
    os.environ["SKIP_INTEGRATION"] = "0"
    runner.warm_up(ROOT, ["tests/test_executor.py"])
    runner.build_executor_tables()
    size = runner.save_executor_rows(target)
    tables = sum(len(group) for group in runner.EXECUTOR_TABLES)
    rows = sum(len(table.rows) for group in runner.EXECUTOR_TABLES for table in group.values())
    print(f"{target}: {tables} tables, {rows} rows, {size} bytes")


if __name__ == "__main__":
    main()
