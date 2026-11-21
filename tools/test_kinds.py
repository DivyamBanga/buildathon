"""Checks for the rules about kinds of change that one test run cannot settle.

    python tools/test_kinds.py [PINNED_SQLGLOT_DIR]

The first part needs nothing but this repository. The second part analyses the
seed patches in tools/kinds_seeds/ (each one was a commit for which the selector
left out a test that pytest fails) against the pinned tree; it runs when that
tree is found (the argument, or /sqlglot) and says so when it is not.
"""
import os
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from selector import analyze, pydiff  # noqa: E402
from selector.covmap import CovMap  # noqa: E402

MAPS = os.path.join(HERE, "..", "maps")
checks = 0


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        raise SystemExit(f"FAIL {label}: got {got!r}, want {want!r}")


# ---- D: a path in more than one section ------------------------------------


def section(old, new, body="@@ -1 +1 @@\n-a\n+b\n"):
    return f"diff --git a/{old} b/{new}\n--- a/{old}\n+++ b/{new}\n{body}"


def plan_of(text, root="/nonexistent"):
    return analyze.analyze("x", text, CovMap(MAPS), root, frozenset())


two = section("sqlglot/a.py", "sqlglot/a.py") + section("sqlglot/b.py", "sqlglot/b.py") + section("sqlglot/a.py", "sqlglot/a.py")
expect("D: same file twice", plan_of(two).select_all, True)
expect("D: note", any("more than one section" in note for note in plan_of(two).notes), True)
rename_edit = section("sqlglot/a.py", "sqlglot/c.py", "") + section("sqlglot/c.py", "sqlglot/c.py")
expect("D: renamed and edited", plan_of(rename_edit).select_all, True)
edit_then_rename = section("sqlglot/c.py", "sqlglot/c.py") + section("sqlglot/c.py", "sqlglot/d.py", "")
expect("D: edited and renamed", plan_of(edit_then_rename).select_all, True)
deleted_created = (
    "diff --git a/sqlglot/a.py b/sqlglot/a.py\ndeleted file mode 100644\n--- a/sqlglot/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
    "diff --git a/sqlglot/a.py b/sqlglot/a.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/a.py\n@@ -0,0 +1 @@\n+b\n"
)
expect("D: deleted and created", plan_of(deleted_created).select_all, True)
swap = section("sqlglot/a.py", "sqlglot/b.py", "") + section("sqlglot/b.py", "sqlglot/a.py", "")
expect("D: swapped names", plan_of(swap).select_all, True)
expect("D: dotted path", plan_of(section("sqlglot/./a.py", "sqlglot/./a.py") + section("sqlglot/a.py", "sqlglot/a.py")).select_all, True)
distinct = section("sqlglot/a.py", "sqlglot/a.py") + section("sqlglot/b.py", "sqlglot/b.py")
expect("D: two files are not repeated", any("more than one section" in note for note in plan_of(distinct).notes), False)

# ---- the seed patches --------------------------------------------------------

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/sqlglot"
if not os.path.isdir(os.path.join(ROOT, "sqlglot")):
    print(f"{checks} checks passed; the seed patches were not analysed (no pinned tree at {ROOT})")
    raise SystemExit(0)

covmap = CovMap(MAPS)
ID = {test_id: bit for bit, test_id in enumerate(covmap.ids)}


def seed(name):
    with open(os.path.join(HERE, "kinds_seeds", name + ".patch"), encoding="utf-8", newline="") as handle:
        return analyze.analyze(name, handle.read(), covmap, ROOT, frozenset())


def selected(plan, test_id):
    return plan.select_all or bool(plan.candidates >> ID[test_id] & 1)


for name in ("D_two_sections_a", "D_two_sections_b", "D_two_sections_c"):
    expect("seed " + name, seed(name).select_all, True)

print(f"{checks} checks passed")
