"""Measure what each test costs to run with the selector's own runner.

    python calibrate.py /data/tests.json /repo/maps /out/runcost.json

Each test class runs in a child forked from a warm parent, exactly as the
selector runs it, and the CPU time of the child and everything it starts is
read around every test. The result weighs "run this test to find out" against
"select it unchecked"; it never decides whether a test fails.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/repo")
import psutil  # noqa: E402

from selector import runner  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
ids = json.load(open(os.path.join(sys.argv[2], "tests.json")))
os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up("/sqlglot", list(dict.fromkeys(test["file"] for test in tests)))


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


groups = {}
for bit, test_id in enumerate(ids):
    groups.setdefault(test_id.rsplit("::", 1)[0], []).append(bit)

costs = [None] * len(ids)
setup = {}
for name, bits in groups.items():
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
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
        runner.run_tests([(bit, ids[bit]) for bit in bits],
                         lambda text: failed.append(text) if text.startswith("R") and text.endswith(" 1\n") else None)
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
    for bit, value in data["marks"].items():
        costs[int(bit)] = round(value, 4)
    setup[name] = round(data["setup"] or 0.0, 4)
    extra = data["total"] - sum(data["marks"].values()) - (data["setup"] or 0)
    print(f"{name}: {len(bits)} tests, setup {setup[name]:.2f}s, tests {sum(data['marks'].values()):.2f}s, "
          f"other {extra:.2f}s, failed {len(data['failed'])}", flush=True)
json.dump({"tests": costs, "setup": setup}, open(sys.argv[3], "w"), separators=(",", ":"))
recorded = sum(test["seconds"] for test in tests)
print(f"total measured {sum(c for c in costs if c):.1f}s vs recorded {recorded:.1f}s")
