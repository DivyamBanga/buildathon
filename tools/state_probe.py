"""Show what a patch changes at import (selector/state.py), without running any test.

    python tools/state_probe.py [patch file ...]

With no patch, the pinned tree is compared with itself: there must be no difference.
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/repo")
from selector import runner, state  # noqa: E402
from selector.analyze import analyze  # noqa: E402
from selector.covmap import CovMap  # noqa: E402

ROOT = "/sqlglot"
state.MAX_DIFFERENCES = int(os.environ.get("BR_MAX_DIFFERENCES", state.MAX_DIFFERENCES))
tests = json.load(open("/data/tests.json"))["tests"]
os.environ["SKIP_INTEGRATION"] = "0"
runner.warm_up(ROOT, list(dict.fromkeys(test["file"] for test in tests)))
loaded = [name for name in sys.modules if name == "sqlglot" or name.startswith("sqlglot.")]
covmap = CovMap("/repo/maps")
state.load_order("/repo/maps/order.json")
warm = {os.path.join(ROOT, path) for path in covmap.warm_files()}

for patch_path in sys.argv[1:] or [None]:
    changes = {}
    plan = None
    if patch_path:
        text = open(patch_path, encoding="utf-8", newline="").read()
        plan = analyze(os.path.basename(patch_path)[:-6], text, covmap, ROOT, warm)
        changes = state.plan_changes(plan)
    overlay = "/tmp/state-probe"
    shutil.rmtree(overlay, ignore_errors=True)
    runner.build_overlay(ROOT, overlay, changes)
    pid = os.fork()
    if pid == 0:
        started = time.process_time()
        result = state.compare_trees(ROOT, overlay, loaded)
        spent = time.process_time() - started
        print(f"== {patch_path or 'no patch'}: {len(result['differences'])} differences, "
              f"{result['inconclusive'] or 'conclusive'}, {spent:.2f} CPU s {result.get('cpu')}", flush=True)
        for row in result["differences"][:int(os.environ.get("BR_SHOW", "40"))]:
            print("   ", row, flush=True)
        if plan is not None:
            own = {(path, first) for path, entries in plan.units.items() for first, _ in entries}
            narrowed = covmap.state_candidates(result, own)
            if narrowed is not None:
                for path, first, lines in plan.warm_units:
                    narrowed |= covmap.test_time(path, first, lines)
                narrowed |= plan.rest_candidates
            floor = plan.import_floor
            print(f"    candidates before {plan.candidates.bit_count()}, after "
                  f"{'-' if narrowed is None else narrowed.bit_count()}, best case "
                  f"{'?' if floor is None else (floor | plan.rest_candidates).bit_count()}; "
                  f"{'; '.join(plan.notes)[:200]}", flush=True)
        os._exit(0)
    os.waitpid(pid, 0)
