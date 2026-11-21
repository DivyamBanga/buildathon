"""Build maps/order.json: which attributes hold tables whose order is the same in every fresh import.

    python tools/build_order.py OUT.json [runs]            (in the lab image, pinned sqlglot at /sqlglot)

The comparison of import state (selector/state.py) may treat the order of a
table as irrelevant only where it already differs from run to run. Here the
whole package is imported in `runs` fresh processes, each with its own
PYTHONHASHSEED and a different amount of memory allocated first (class sets
are ordered by address), and for every module or class attribute the order
of every dict, and of every list or tuple of classes, inside it is recorded
(state.order_signature). An attribute with something to order whose record
is identical in all runs is "stable": the comparison then demands the same
order. The imports go the way the coordinator imports the pinned package
(runner.warm_up, then state.load_pinned).
"""
import hashlib
import json
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
ROOT = os.environ.get("BR_SQLGLOT", "/sqlglot")


def one_run(tests_json: str, out: str) -> None:
    junk = [bytearray(random.randrange(1, 4000)) for _ in range(random.randrange(100, 4000))]  # noqa: F841
    from selector import runner, state

    tests = json.load(open(tests_json))["tests"]
    os.environ["SKIP_INTEGRATION"] = "0"
    runner.warm_up(ROOT, list(dict.fromkeys(test["file"] for test in tests)))
    state.load_pinned(ROOT)
    modules = state.PINNED[2]
    result = {}
    for module, qualname, attribute, value in state.iter_attributes(modules):
        text = state.order_signature(value)
        if text:
            result[f"{module}\t{qualname}\t{attribute}"] = hashlib.md5(text.encode()).hexdigest()
    with open(out, "w") as handle:
        json.dump(result, handle)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--run":
        one_run(sys.argv[2], sys.argv[3])
        return
    out = sys.argv[1]
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    tests_json = os.environ.get("BR_TESTS_JSON", "/w/tests.json")
    records = []
    for index in range(runs):
        target = f"/tmp/order-run-{os.getpid()}-{index}.json"
        env = dict(os.environ, PYTHONHASHSEED=str(random.randrange(1, 4294967295)))
        subprocess.run([sys.executable, os.path.abspath(__file__), "--run", tests_json, target], check=True, env=env)
        records.append(json.load(open(target)))
        os.remove(target)
    keys = set().union(*records)
    stable = {}
    unstable = 0
    for key in sorted(keys):
        values = [record.get(key) for record in records]
        if all(value is not None and value == values[0] for value in values):
            module, qualname, attribute = key.split("\t")
            stable.setdefault(module, {}).setdefault(qualname, []).append(attribute)
        else:
            unstable += 1
    count = sum(len(attributes) for classes in stable.values() for attributes in classes.values())
    with open(out, "w") as handle:
        json.dump({"runs": runs, "stable": stable}, handle, separators=(",", ":"), sort_keys=True)
    print(f"{runs} runs: {len(keys)} attributes with something to order, {count} stable, {unstable} not")
    with open(out + ".unstable.txt", "w") as handle:
        for key in sorted(keys):
            values = [record.get(key) for record in records]
            if not all(value is not None and value == values[0] for value in values):
                handle.write(key.replace("\t", " | ") + "\n")


if __name__ == "__main__":
    main()
