# Blast Radius entry

Given a patch to sqlglot at commit `eb11aec`, pick the tests that can catch it
and split them across workers. This entry does not predict which tests fail: it
works out which tests could possibly notice the change, **runs those against
the patched code**, and selects the ones that fail.

## How a commit is decided

1. **Apply the patch in memory** (`selector/patchlib.py`). A hunk that does not
   apply exactly makes the commit fall back to running every test.

2. **Find what really changed** (`selector/pydiff.py`). The old and new file
   are compiled and their code objects compared. Line numbers, docstrings,
   string annotations and Python 3.13's per-class bookkeeping constants are
   ignored, so a comment, a blank line or a reformatting changes nothing and
   the commit selects nothing. Otherwise the result is a list of changed code
   objects: a function or lambda (runs only when called) or a module or class
   body (ran at import). Inside a changed function, the old and new source of
   that function are parsed and compared statement by statement, to name the
   old lines a test must execute to see the change.

3. **Name the candidate tests** (`selector/analyze.py`, `selector/covmap.py`).
   `maps/` records, for every test run alone at the pinned commit, each line it
   executed (per code object, child processes included), each file it opened
   and each class it instantiated. A test is a candidate only if

   - it executed a changed statement of a changed function, or
   - it opened a changed fixture file, or
   - for a change to a class body: it built an instance of that class or of a
     subclass, or executed a line naming it (for the classes of a dialect: it
     used that dialect or one built on it); and, when the change only gives a
     constant a new value, it also executed a line that reads that constant, or
   - for any other import-time change: it imported the file.

   No candidates means the commit selects nothing.

4. **Run the candidates against the patch** (`selector/runner.py`). The
   coordinator imports sqlglot and the test modules once, then forks a child
   per commit:

   - *hot*: when only function bodies changed, the child swaps the new code
     objects into the functions already in memory and runs the tests at once.
   - *clean*: when import-time code changed, the child drops sqlglot and the
     tests from `sys.modules` and imports them again from an overlay tree of
     symlinks plus the patched files. It first imports every test file that
     imports a changed module: a file that can no longer be imported fails all
     of its tests, as it does under pytest.

   Tests run under `unittest` with fail-fast, so a failing test stops at its
   first failure. A test that hangs is killed and counted as failing.

   Fixture-driven tests (the optimizer `check_file` tests, the TPC-H and TPC-DS
   executor tests, `identity.sql`) loop over independent cases. The map also
   records which lines each case reaches, so only the cases that reach the
   changed code, plus any case the patch itself edits, are run.

5. **Weigh running against selecting** (`selector/main.py`). Running a test
   costs CPU seconds; selecting it costs its recorded seconds on a worker. A
   test is selected unchecked when running it would cost more than selecting
   it. Tests are run cheapest first; once one fails the commit is known to be
   real, and cheap remaining candidates are selected rather than run. What a
   test costs to run here is measured as the batch goes.

6. **Split across workers** (`selector/schedule.py`): the split that minimises
   busiest worker + 2 s per worker.

If anything goes wrong (a crash, a timeout, the wall-clock deadline), the
affected commit selects every candidate it has not verified, and a commit that
cannot be analysed at all selects every test. A test is never left out on a
guess.

## What is precomputed

Everything in `maps/` is built from the pinned commit's own tests, with the
tools in `tools/` (they run in the labeling image, `tools/lab.Dockerfile`):

| File | Made by | Content |
| --- | --- | --- |
| `cov.bin`, `cov.json` | `trace_all.py`, `build_map.py` | (code object, line) → tests that executed it |
| `opens.json` | same | fixture file → tests that open it |
| `funcs.json` | `build_map.py` | first and last line of every `def` |
| `warm.json` | `build_map.py` | code that already ran once the test modules are imported |
| `uses.bin`, `mentions.bin`, `classes.json`, `families.json` | `build_extra.py` | class → tests that instantiate or name it |
| `readers.bin`, `constants.json` | `build_extra.py` | class constant → tests that execute a line reading it |
| `cases.bin`, `cases.json`, `casesrc.json` | `trace_cases.py`, `build_cases.py` | per fixture case, the lines it reaches |
| `setup.json` | `trace_setup.py` | what the executor tests' class setup executes and reads |
| `runcost.json` | `calibrate.py` | CPU seconds each test takes in this runner |

There is no table from a patch, or from a kind of edit, to failing tests: the
maps describe the pinned commit only, and every selected test was either
observed failing on the patched code or could not be ruled out.

## How it was checked

`tools/gt_run.py` labels a patch the way the organisers did (apply to a fresh
checkout, run the whole suite with pytest); it reproduces `sample/answers.json`
exactly. `tools/mutate.py` generates patches of many kinds (operator and
constant edits, fixture edits, comments, multi-file changes, test edits,
deleted and swapped statements), which were labeled this way and used to check
that the selector never leaves out a failing test. Those patches and labels
are development data only; nothing derived from them is in the image.

## Layout

```
Dockerfile          the submission image
selector/           the program that runs in it
maps/               the dependency map (built once, committed)
tools/              how the map was built, and the validation tooling
sample/, score.py   the organisers' sample set and scorer
kit/                the untouched starter files
```

## Running it

```sh
make sample                  # build, run on the 30 sample commits, score
tools/run_sample.sh sample   # the same, plus the container's CPU seconds
```
