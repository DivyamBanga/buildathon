"""Checks for the rule about pyproject.toml and setup.cfg (selector/analyze.py).

    python tools/test_cfg.py [PINNED_SQLGLOT_DIR]

Needs pytest (it runs pytest's own loaders in a separate interpreter), so run it
in the lab image. The first part uses a small root written here; the second
applies the seed patches in tools/cfg_seeds/ to the pinned tree when it is found.
"""
import difflib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from selector import analyze  # noqa: E402
from selector.covmap import CovMap  # noqa: E402

MAPS = os.path.join(HERE, "..", "maps")
covmap = CovMap(MAPS)
checks = 0


def expect(label, got, want):
    global checks
    checks += 1
    if got != want:
        raise SystemExit(f"FAIL {label}: got {got!r}, want {want!r}")


PYPROJECT = '[project]\nname = "x"\ndependencies = ["a>=1"]\n\n[tool.ruff]\nline-length = 120\n'
SETUP = "[mypy]\ndisallow_untyped_calls = False\nno_implicit_optional = True\n"
FILES = {
    "pyproject.toml": PYPROJECT,
    "setup.cfg": SETUP,
    "sqlglotc/pyproject.toml": PYPROJECT,
    "tests/setup.cfg": SETUP,
}


def edit(path, old, new):
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}"))
    return f"diff --git a/{path} b/{path}\n{body}"


def create(path, text):
    body = "".join(difflib.unified_diff([], text.splitlines(keepends=True), "/dev/null", f"b/{path}"))
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n{body}"


def delete(path):
    body = "".join(difflib.unified_diff(FILES[path].splitlines(keepends=True), [], f"a/{path}", "/dev/null"))
    return f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n{body}"


with tempfile.TemporaryDirectory() as root:
    for path, text in FILES.items():
        os.makedirs(os.path.join(root, os.path.dirname(path)), exist_ok=True)
        with open(os.path.join(root, path), "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def plan_of(text):
        return analyze.analyze("x", text, covmap, root, frozenset())

    def everything(label, text, want):
        expect(label, plan_of(text).select_all, want)

    # broken: pytest stops before collection
    everything("pyproject: table header open", edit("pyproject.toml", PYPROJECT, PYPROJECT.replace("[project]", "[project")), True)
    everything("pyproject: string left open", edit("pyproject.toml", PYPROJECT, PYPROJECT.replace('"x"', '"x')), True)
    everything("pyproject: key twice", edit("pyproject.toml", PYPROJECT, PYPROJECT.replace('name = "x"\n', 'name = "x"\nname = "y"\n')), True)
    everything("setup.cfg: line without =", edit("setup.cfg", SETUP, SETUP.replace("no_implicit_optional = True", "no_implicit_optional")), True)
    everything("setup.cfg: section header open", edit("setup.cfg", SETUP, SETUP.replace("[mypy]", "[mypy")), True)
    everything("tests/setup.cfg broken (pytest searches tests/)", edit("tests/setup.cfg", SETUP, SETUP.replace("[mypy]", "[mypy")), True)
    # valid edits: nothing
    everything("pyproject: version bound", edit("pyproject.toml", PYPROJECT, PYPROJECT.replace(">=1", ">=2")), False)
    everything("pyproject: ruff line", edit("pyproject.toml", PYPROJECT, PYPROJECT.replace("120", "100")), False)
    everything("setup.cfg: mypy flag", edit("setup.cfg", SETUP, SETUP.replace("False", "True")), False)
    everything("pyproject: comment line", edit("pyproject.toml", PYPROJECT, PYPROJECT + "# a note\n"), False)
    everything("setup.cfg: comment line", edit("setup.cfg", SETUP, SETUP + "# a note\n"), False)
    # pytest settings appear
    everything("pyproject: ini_options added", edit("pyproject.toml", PYPROJECT, PYPROJECT + '\n[tool.pytest.ini_options]\naddopts = "-x"\n'), True)
    everything("setup.cfg: [tool:pytest] added", edit("setup.cfg", SETUP, SETUP + "\n[tool:pytest]\naddopts = -x\n"), True)
    everything("setup.cfg: [pytest] section (pytest fails on it)", edit("setup.cfg", SETUP, SETUP + "\n[pytest]\nx = 1\n"), True)
    everything("pyproject: [tool.pytest] by dotted key, no word in the lines", edit("pyproject.toml", PYPROJECT, PYPROJECT.replace("[tool.ruff]", "[tool.ruff]\n[tool.\"pyt\".x]")), False)
    # appears, vanishes, moves
    everything("pyproject deleted", delete("pyproject.toml"), True)
    everything("setup.cfg deleted", delete("setup.cfg"), True)
    everything("new tests/pytest.ini", create("tests/pytest.ini", "[pytest]\n"), True)
    everything("new pytest.toml", create("pytest.toml", "[pytest]\n"), True)
    everything("new .pytest.toml in tests/", create("tests/.pytest.toml", "[pytest]\n"), True)
    everything("new tests/pyproject.toml", create("tests/pyproject.toml", PYPROJECT), True)
    everything("new tests/dialects/setup.cfg", create("tests/dialects/setup.cfg", SETUP), True)
    rename = (
        "diff --git a/pyproject.toml b/other.toml\nsimilarity index 100%\nrename from pyproject.toml\nrename to other.toml\n"
    )
    everything("pyproject renamed away", rename, True)
    # a file pytest never looks at keeps today's behaviour (the 'no test opens it' path)
    plan = plan_of(edit("sqlglotc/pyproject.toml", PYPROJECT, PYPROJECT.replace("[project]", "[project")))
    expect("sqlglotc/pyproject.toml broken: not selected", plan.select_all, False)
    expect("sqlglotc/pyproject.toml broken: no config note", any("pytest" in n for n in plan.notes), False)
    # BOM: tomllib refuses it; the real loader decides for each
    bom = "﻿"
    plan = plan_of(edit("pyproject.toml", PYPROJECT, bom + PYPROJECT))
    expect("pyproject: BOM added", plan.select_all, True)
    plan = plan_of(edit("setup.cfg", SETUP, bom + SETUP))
    # iniconfig reads the BOM as part of the first line and raises nothing (checked with real pytest)
    expect("setup.cfg: BOM added", plan.select_all, False)
    # a file that is not UTF-8 never reaches the loader (binary path)
    with open(os.path.join(root, "setup.cfg"), "wb") as handle:
        handle.write(b"[mypy]\nx = \xff\n")
    expect("setup.cfg not UTF-8 at the pinned commit", plan_of(edit("setup.cfg", SETUP, SETUP + "y = 1\n")).select_all, True)
    # two sections for one file
    two = edit("pyproject.toml", PYPROJECT, PYPROJECT.replace("120", "100")) * 2
    everything("pyproject twice", two, True)
    # the loader cannot start: everything is selected
    saved = analyze._CFG_PROBE
    analyze._CFG_PROBE = "raise SystemExit(3)"
    with open(os.path.join(root, "setup.cfg"), "w", encoding="utf-8", newline="") as handle:
        handle.write(SETUP)
    everything("probe fails: valid edit selects everything", edit("setup.cfg", SETUP, SETUP.replace("False", "True")), True)
    analyze._CFG_PROBE = saved

# ---- seeds against the pinned tree ------------------------------------------

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/sqlglot"
if not os.path.isdir(os.path.join(ROOT, "sqlglot")):
    print(f"{checks} checks passed; the seed patches were not analysed (no pinned tree at {ROOT})")
    raise SystemExit(0)
for name in sorted(os.listdir(os.path.join(HERE, "cfg_seeds"))):
    with open(os.path.join(HERE, "cfg_seeds", name), encoding="utf-8", newline="") as handle:
        plan = analyze.analyze(name, handle.read(), covmap, ROOT, frozenset())
    expect("seed " + name, plan.select_all, True)
print(f"{checks} checks passed")
