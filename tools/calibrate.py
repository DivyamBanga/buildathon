"""Measure what each test costs to run with the selector's own runner.

    python calibrate.py /data/tests.json /repo/maps /out/runcost.json

Each test class runs in a child forked from a warm parent, exactly as the
selector runs it, and the CPU time of the child and everything it starts is
read around every test. The executor tests are measured a second time the way
they run when their class setup can be reused (prebuilt tables, recorded
DuckDB answers). The result weighs "run this test to find out" against "select
it unchecked"; it never decides whether a test fails.
"""
import json
import os
import sys

sys.path.insert(0, "/repo")
import psutil  # noqa: E402

from selector import runner  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
maps = sys.argv[2]
ids = json.load(open(os.path.join(maps, "tests.json")))
os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up("/sqlglot", list(dict.fromkeys(test["file"] for test in tests)))
# As the coordinator does: what is imported is never collected again, and the
# children collect with the runner's thresholds.
import gc  # noqa: E402

gc.collect()
gc.freeze()
EXECUTOR = "tests/test_executor.py::TestExecutor"


def tree_cpu():
    me = psutil.Process()
    times = os.times()
    total = times.user + times.system + times.children_user + times.children_system
    for child in me.children(recursive=True):
        try:
            cpu = child.cpu_times()
            total += cpu.user + cpu.system
        except psutil.Error:
            pass
    return total


def measure(name, bits, prepare=None):
    """Run one class's tests in a forked child. Returns ({bit: cpu}, setup cpu)."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        if runner.GC_THRESHOLD:
            gc.set_threshold(*runner.GC_THRESHOLD)
        if prepare is not None:
            prepare()
        marks = {}
        began = tree_cpu()
        first = [None]
        original_start = runner._Result.startTest
        original_stop = runner._Result.stopTest

        def start(self, test):
            now = tree_cpu()
            if first[0] is None:
                first[0] = now - began
            marks[test._br_index] = now
            original_start(self, test)

        def stop(self, test):
            original_stop(self, test)
            marks[test._br_index] = tree_cpu() - marks[test._br_index]

        runner._Result.startTest = start
        runner._Result.stopTest = stop
        failed = []
        runner.run_tests(
            [(bit, ids[bit]) for bit in bits],
            lambda text: failed.append(text) if text.startswith("R") and text.endswith(" 1\n") else None,
        )
        total = tree_cpu() - began
        os.write(write_fd, json.dumps({"marks": marks, "setup": first[0], "total": total, "failed": failed}).encode())
        os._exit(0)
    os.close(write_fd)
    chunks = []
    while True:
        chunk = os.read(read_fd, 1 << 16)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    os.waitpid(pid, 0)
    data = json.loads(b"".join(chunks))
    costs = {int(bit): round(value, 4) for bit, value in data["marks"].items()}
    setup = round(data["setup"] or 0.0, 4)
    extra = data["total"] - sum(costs.values()) - setup
    print(f"{name}: {len(bits)} tests, setup {setup:.2f}s, tests {sum(costs.values()):.2f}s, "
          f"other {extra:.2f}s, failed {len(data['failed'])}", flush=True)
    return costs, setup


groups = {}
for bit, test_id in enumerate(ids):
    groups.setdefault(test_id.rsplit("::", 1)[0], []).append(bit)

costs = [None] * len(ids)
setup = {}
for name, bits in groups.items():
    if os.environ.get("BR_CALIBRATE_ONLY"):
        break
    measured, setup[name] = measure(name, bits)
    for bit, value in measured.items():
        costs[bit] = value

fast = {"tests": {}, "setup": {}}
if EXECUTOR in groups and os.environ.get("BR_CALIBRATE_ONLY") == "answers":
    runner.load_duckdb_answers(os.path.join(maps, "duckdb.bin"))
elif EXECUTOR in groups:
    runner.build_executor_tables()
    runner.load_duckdb_answers(os.path.join(maps, "duckdb.bin"))
    measured, fast["setup"][EXECUTOR] = measure(EXECUTOR + " (setup reused)", groups[EXECUTOR], runner._use_executor_tables)
    fast["tests"] = {str(bit): value for bit, value in measured.items()}

memo = {"tests": {}}
if EXECUTOR in groups and os.path.exists(os.path.join(maps, "execmemo.bin")) and not os.environ.get("BR_CALIBRATE_ONLY"):
    runner.load_executor_memo(os.path.join(maps, "execmemo.bin"))

    def with_memo():
        runner._use_executor_tables()
        runner.install_executor_memo()

    measured, _ = measure(EXECUTOR + " (results reused)", groups[EXECUTOR], with_memo)
    memo["tests"] = {str(bit): value for bit, value in measured.items()
                     if ids[bit].rsplit("::", 1)[-1] in runner.MEMO_TESTS}

answers = {"tests": {}}
if EXECUTOR in groups and os.path.exists(os.path.join(maps, "ducksetup.json")):
    # Own class setup, DuckDB's answers reused: how the executor tests run after
    # a clean import, or when the patch touches something their setup runs.
    runner.load_duckdb_setups(os.path.join(maps, "ducksetup.json"))
    if runner.DUCKDB_ANSWERS is None:
        runner.load_duckdb_answers(os.path.join(maps, "duckdb.bin"))
    runner.prepare_duck_probe()
    measured, answers["setup"] = measure(
        EXECUTOR + " (own setup, answers reused)", groups[EXECUTOR],
        lambda: runner._install_duck_probe(("/sqlglot",), set()),
    )
    answers["tests"] = {str(bit): value for bit, value in measured.items()}

result = {"tests": costs, "setup": setup, "fast": fast, "memo": memo, "answers": answers}
if os.environ.get("BR_CALIBRATE_ONLY") == "answers":
    # Keep every other figure of an existing file: only this measurement is new.
    result = json.load(open(sys.argv[3]))
    result["answers"] = answers
json.dump(result, open(sys.argv[3], "w"), separators=(",", ":"))
recorded = sum(test["seconds"] for test in tests)
print(f"total measured {sum(c for c in costs if c):.1f}s vs recorded {recorded:.1f}s")
