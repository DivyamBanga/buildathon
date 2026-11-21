"""Build maps/dirs.json: which tests could notice a file appearing in or vanishing from a directory.

    python build_dirs.py /data/tests.json /out/names /out/maps [/out/trace_classes] [/out/suite]
    python build_dirs.py /data/tests.json - /out/maps /out/trace_classes        (without the names traces)

/out/names holds one file per test from tools/trace_names_run.py (each test run alone
under tools/trace_names/sitecustomize.py). A file that appears next to others changes
what a test sees in two ways, and the map keeps both apart:

  explicit   directory -> tests that listed it themselves (os.listdir, os.scandir, glob,
             pkgutil.iter_modules, importlib.metadata, ...): they see every new entry.
  names      module name -> tests whose process asked the import system for that name
             (found or not, not yet in sys.modules): the import system lists a directory
             through importlib's FileFinder, and a new file matters to it only under the
             name somebody asks for. Pytest's own collection lists directories too and
             keeps only what its arguments name, so it is not counted.
  unchecked  the part of `explicit` whose listing code is outside sqlglot/ and tests/
             (sqlglotc/setup.py reaches the checkout through the real location of its own
             file, past the overlay a child runs in): such tests cannot be run faithfully.
  roots      the directories of the checkout that were on sys.path: module names are
             formed from them.

The traces written by trace_all.py hold "D <path>" rows made by the same audit events
without telling who listed; with a third argument each is checked to be one of the rows here.

Without the names traces ("-") only the "D" rows of trace_all.py are known. They do not say who
listed (the import system and pkgutil look alike), and the checkout root is not among them, so
every listed directory counts as explicit, every test lists the root, and "names" is null: the
selector then treats any entry that could be imported as a module as noticed by every test.

  dirs.json  {"roots": [...], "explicit": {...}, "unchecked": {...}, "names": {...}, "everywhere": [...]}
"""
import json
import os
import sys
from collections import defaultdict

tests_path, trace_dir, out_dir = sys.argv[1:4]
old_dir = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "-" else None
suite_dir = sys.argv[5] if len(sys.argv) > 5 else None
ROOT = "/sqlglot"
METADATA = "/importlib/metadata/"
tests = json.load(open(tests_path))["tests"]


def rel(path):
    path = os.path.normpath(path)
    return "" if path == ROOT else path[len(ROOT) + 1:]


if trace_dir == "-":
    if not old_dir:
        sys.exit("the names traces or the trace_all.py traces are needed")
    explicit = defaultdict(set)
    unchecked = defaultdict(set)
    explicit[""] = {test["number"] for test in tests}
    for test in tests:
        for row in open(os.path.join(old_dir, f"{test['number']}.txt"), encoding="utf-8"):
            if row.startswith("D "):
                directory = rel(row[2:].strip())
                explicit[directory].add(test["number"])
                if "sqlglotc/" in row:
                    unchecked[directory].add(test["number"])
    result = {
        "roots": [""],
        "explicit": {path: sorted(numbers) for path, numbers in sorted(explicit.items())},
        "unchecked": {path: sorted(numbers) for path, numbers in sorted(unchecked.items())},
        "names": None,
        "everywhere": [],
    }
    os.makedirs(out_dir, exist_ok=True)
    json.dump(result, open(os.path.join(out_dir, "dirs.json"), "w"), separators=(",", ":"))
    print("explicit:", {path: len(numbers) for path, numbers in result["explicit"].items()})
    print("unchecked:", {path: len(numbers) for path, numbers in result["unchecked"].items()})
    sys.exit(0)

explicit = defaultdict(set)
unchecked = defaultdict(set)
broken = {}
metadata = defaultdict(set)
callers = defaultdict(set)
names = defaultdict(set)
roots = defaultdict(set)
listed_by_import = defaultdict(set)
listed_by_pytest = defaultdict(set)
missing = []
for test in tests:
    number = test["number"]
    try:
        rows = open(os.path.join(trace_dir, f"{number}.txt"), encoding="utf-8").read().splitlines()
    except OSError:
        missing.append(number)
        continue
    seen = set()
    where = {}
    first_frames = {}
    for row in rows:
        if row.startswith("V "):
            path, _, first = row[2:].partition(" ")
            first_frames.setdefault(rel(path), set()).add(first)
    for row in rows:
        kind, _, rest = row.partition(" ")
        if kind == "W":
            path, _, caller = rest.partition(" ")
            where.setdefault(rel(path), set()).add(caller)
        elif kind == "V":
            path, _, first = rest.partition(" ")
            callers[rel(path)].add(first)
        elif kind == "R":
            status = rest.split()[0]
            if status != "0":
                broken[number] = status
    for row in rows:
        kind, _, rest = row.partition(" ")
        if kind == "N":
            names[rest].add(number)
        elif kind == "S":
            roots[rel(rest)].add(number)
        elif kind == "D":
            directory = rel(rest)
            if first_frames.get(directory) and all(METADATA in frame for frame in first_frames[directory]):
                metadata[directory].add(number)
                seen.add(directory)
                continue
            explicit[directory].add(number)
            if any(not caller.startswith((ROOT + "/sqlglot/", ROOT + "/tests/")) for caller in where.get(directory, ())):
                unchecked[directory].add(number)
            seen.add(directory)
        elif kind == "I":
            listed_by_import[rel(rest)].add(number)
            seen.add(rel(rest))
        elif kind == "P":
            listed_by_pytest[rel(rest)].add(number)
            seen.add(rel(rest))
    if old_dir:
        for row in open(os.path.join(old_dir, f"{number}.txt"), encoding="utf-8"):
            if row.startswith("D "):
                directory = rel(row[2:].strip())
                if directory not in seen:
                    print("old D row not in the new trace:", number, directory)

if missing:
    sys.exit(f"{len(missing)} tests have no trace, first {missing[:5]}")

everywhere = sorted(name for name, numbers in names.items() if len(numbers) == len(tests))
if suite_dir:
    # One pytest process over the whole suite (tools/trace_names_run.py's tracer, a single
    # run): a name asked there that no single-test run asked depends on the tests before it
    # (a module that is imported lazily, a registry that an earlier test emptied). It counts
    # as asked by every test, and a directory listed there but by no test alone is reported.
    suite_names = set()
    suite_listed = set()
    for entry in os.listdir(suite_dir):
        for row in open(os.path.join(suite_dir, entry), encoding="utf-8").read().splitlines():
            kind, _, rest = row.partition(" ")
            if kind == "N":
                suite_names.add(rest)
            elif kind == "D":
                suite_listed.add(rel(rest))
    only_suite = sorted(suite_names - set(names))
    print("names asked only in the whole-suite run (now asked by everyone):", only_suite)
    print("directories listed only in the whole-suite run:", sorted(suite_listed - set(explicit) - set(metadata)))
    everywhere = sorted(set(everywhere) | set(only_suite))
result = {
    "roots": sorted(root for root, numbers in roots.items() if len(numbers) * 2 > len(tests)),
    "broken": sorted(broken),
    "metadata": {path: sorted(numbers) for path, numbers in sorted(metadata.items())},
    "explicit": {path: sorted(numbers) for path, numbers in sorted(explicit.items())},
    "unchecked": {path: sorted(numbers) for path, numbers in sorted(unchecked.items())},
    "names": {name: sorted(numbers) for name, numbers in sorted(names.items()) if len(numbers) != len(tests)},
    "everywhere": everywhere,
}
os.makedirs(out_dir, exist_ok=True)
json.dump(result, open(os.path.join(out_dir, "dirs.json"), "w"), separators=(",", ":"))
print("roots", result["roots"])
print("explicit:", {path: len(numbers) for path, numbers in result["explicit"].items()})
print("unchecked:", {path: len(numbers) for path, numbers in result["unchecked"].items()})
print("importlib lists", {path: len(numbers) for path, numbers in sorted(listed_by_import.items())})
print("pytest lists", {path: len(numbers) for path, numbers in sorted(listed_by_pytest.items())})
print("metadata (importlib.metadata scans, only .dist-info and .egg-info entries matter):", {path: len(numbers) for path, numbers in result["metadata"].items()})
print("broken (exit code, not 0):", broken)
print("callers of D listings:", {path: sorted(frames) for path, frames in sorted(callers.items())})
absent = []
for name in sorted(names):
    for base in result["roots"] or [""]:
        parts = name.split(".")
        stem = os.path.join(ROOT, base, *parts)
        if os.path.isdir(stem) or os.path.exists(stem + ".py"):
            break
    else:
        absent.append(name)
mine = [name for name in absent if name.split(".")[0] in ("sqlglot", "tests", "sqlglotc")]
print("names asked that do not exist in the checkout:", len(absent), "; of them under sqlglot/tests:", len(mine), mine)
print("other names (standard library and installed packages, which a new file at the root would shadow):", len(absent) - len(mine))
print("names", len(result["names"]), "everywhere", len(everywhere))
print("dirs.json", os.path.getsize(os.path.join(out_dir, "dirs.json")), "bytes")
