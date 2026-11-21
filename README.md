# Blast Radius entry

Given a patch to sqlglot at commit `eb11aec`, pick the tests that can catch it
and split them across workers. This entry does not predict which tests fail: it
works out which tests could possibly notice the change, **runs those against
the patched code**, and selects the ones that fail.

## How a commit is decided

1. **Apply the patch in memory** (`selector/patchlib.py`). A hunk that does not
   apply exactly, or a patch the parser does not recognise, makes the commit
   fall back to running every test.

2. **Find what really changed** (`selector/pydiff.py`). The old and new file
   are compiled and their code objects compared instruction by instruction,
   with constants, names and jump targets resolved. Line numbers, docstrings,
   string annotations, the names of local variables and Python 3.13's per-class
   bookkeeping constants are not part of the comparison, so a comment, a blank
   line or a reformatting changes nothing and the commit selects nothing.
   Otherwise the result is a list of changed code objects: a function or lambda
   (runs only when called) or a module or class body (ran at import). Inside a
   changed function, the old and new source of that function are parsed and
   compared statement by statement, to name the old lines a test must execute
   to see the change.

3. **Name the candidate tests** (`selector/analyze.py`, `selector/covmap.py`).
   `maps/` records, for every test run alone at the pinned commit, each line it
   executed (per code object, child processes included), each file it opened
   and each class it instantiated. A test is a candidate only if

   - it executed a changed statement of a changed function, or
   - it opened a changed fixture file, or
   - it opens the changed Python source as a file, to read, parse or run it
     (one test parses the `sqlglot/typing` modules, three run
     `sqlglotc/setup.py`): the text is data to it, whatever lines execute, or
   - for a change to a class body: it built an instance of that class or of a
     subclass, or executed a line naming it (for the classes of a dialect: it
     used that dialect or one built on it); and, when the change only gives a
     constant a new value, it also executed a line that reads that constant, or
   - for any other import-time change: it imported the file.

   No candidates means the commit selects nothing.

   **A closer look at import-time changes** (`selector/state.py`). A module or
   class body runs once, at import, and all it can do is leave the package in
   another state: other module globals, class attributes, defaults or closures.
   When the candidates above would be expensive, a child first imports the
   whole package twice, pinned and patched, and compares the two results
   object by object (functions by their resolved instructions, defaults and
   closures; tables entry by entry; classes attribute by attribute). What comes
   out is the list of (module or class, attribute) pairs that differ, including
   whatever sqlglot itself derived from the changed value while importing.

   - No difference: the change cannot be observed (a flag only the type checker
     reads, a `not` on a value nothing uses), and it adds no candidate.
   - A function that kept its code but got another default or closure value is
     noticed only by calling it: the tests that run that function.
   - A constant with a new value is noticed only by reading it: the tests that
     can reach the module or class and execute a line reading that name.
   - Anything else on a class: the tests that can reach the class, as above.

   "Run" and "read" here mean while the test itself runs. A second line map
   (`tcov.bin`) records each test with sqlglot imported in full beforehand, so
   that what sqlglot executes while importing itself, whichever test runs, is
   not mistaken for the test using it: that part is exactly what the state
   comparison accounts for. The comparison says "different" for anything it
   does not know how to compare, and the candidates fall back to the rule
   above when it is inconclusive (too many differences, state outside the
   package changed). When a module no longer imports at all, every test that
   imports it or a module that depends on it is a candidate, whether or not it
   could "see" the changed class: the comparison is therefore always run
   before tests are left out for that reason.

   What the comparison takes for granted, each checked against the pinned
   code: annotations, docstrings and what `typing` records about a TypedDict
   are not read while tests run; nothing relies on the order of the entries of
   a table keyed by classes, enum members or strings (sqlglot builds several
   of them by looping over sets, so their order already differs from run to
   run); a default or a closure value of a function matters only when the
   function is called. A function that imports modules on demand (four in
   sqlglot) is outside all of this: when it runs depends on which tests ran
   before, so a change to one selects every test. So does a change to a
   function that reads the registry those imports fill (four more, around
   `Dialect._classes`), or to what those functions read to find an entry (the
   module-level code of the two files that hold the names of the dialect
   modules, and the enum whose values are the keys): one test file empties
   that registry, and every later test meets it as the tests before left it.

4. **Run the candidates against the patch** (`selector/runner.py`). The
   coordinator imports sqlglot and the test modules once, then forks a child
   per commit:

   - *hot*: when only function bodies changed (and none of them already ran
     while the tests were imported), the child swaps the new code objects into
     the functions already in memory and runs the tests at once.
   - *on demand*: sqlglot imports most of its dialects only when one is first
     used, so about half of its modules have never run in the coordinator.
     Nothing of such a module exists in the child, whatever the patch changes
     in it (function bodies, class bodies, module-level code): the child stays
     as it is and imports the patched source when a test first asks for the
     module, which is what a fresh process does. The child checks for itself
     that the module is not in `sys.modules`; if it is, the commit is checked
     with a clean import instead.
   - *clean*: when import-time code of an already imported module changed, the
     child drops sqlglot and the tests from `sys.modules` and imports them
     again from an overlay tree of symlinks plus the patched files. It first
     imports every test file that imports a changed module: a file that can no
     longer be imported fails all of its tests, as it does under pytest.

   A test that reaches a patched file through the file system must find the
   patched text too. In a child that keeps the pinned modules, `open` and
   `io.open_code` (which `runpy` reads code through) are redirected for every
   patched file. After a clean import the tree is an overlay of symlinks, and a
   test that resolves its own location would follow them out into the pinned
   checkout, so a resolved path under the pinned checkout is handed back as
   the same path under the overlay.

   Tests run under `unittest` with fail-fast, so a failing test stops at its
   first failure. A test that hangs, burns far more CPU than it normally costs
   or keeps allocating memory is stopped and counted as failing.

   CPU time is what the score counts, and there is wall-clock time to spare, so
   nothing runs side by side unless the batch is running late: children run one
   at a time, and the process pools some tests start are replaced by a stand-in
   that does the work in the test's own process, each call when its result is
   asked for (arguments and results are still pickled, as a pool would). A
   test that fails on its third query then never pays for the other ninety.

5. **Do not repeat work the patch cannot have changed.**

   - Fixture-driven tests (the optimizer `check_file` tests, the TPC executor
     tests, `identity.sql`, the dialect loop of `test_generator_memory`) loop
     over independent cases. The map records which lines each case reaches, so
     only the cases that reach the changed code, plus any case the patch itself
     edits, are run.
   - The executor tests compare sqlglot's executor with DuckDB. DuckDB's answer
     depends on the SQL text it is given and on what the connection holds, and
     what a connection holds is decided by the statements it was sent. The
     answers are recorded once per text, together with the statements the
     pinned class setup sends. In a child, `duckdb.connect()` returns a
     stand-in that watches what it is sent: while that is, one for one, a
     recorded setup (the same views over the same unchanged files), DuckDB is
     not even opened, and once the whole setup has been sent a recorded query
     gets its recorded answer. Anything else opens the real connection, sends
     it what was held back, and is passed on; after a statement that is
     neither recognised nor a plain SELECT nothing is answered from the record.
     This holds for any patch, because it is decided by what the patched code
     really sends. The optimizer tests' small DuckDB database is held back the
     same way until a case executes a query.
   - `sqlglot.executor.execute` prepares the tables it was given, optimizes
     the query, plans it and runs the plan. For a patch that changes none of
     the lines `execute` runs outside the optimizer, a TPC query whose
     optimized form is exactly the pinned one is not planned and executed
     again: its recorded result is returned.
   - The executor tests' class setup parses every TPC table from CSV. For a
     patch that touches nothing that setup runs, the tables are built once and
     shared by the children.

6. **Weigh running against selecting** (`selector/main.py`). Running a test
   costs CPU seconds; selecting it costs its recorded seconds on a worker. A
   test is selected unchecked when running it would cost more than selecting
   it. Tests are run cheapest first; before an expensive check, a few cheap
   tests are run as probes. Once one fails the commit is known to be real, and
   remaining candidates are selected rather than run unless checking them is
   clearly cheaper. What a test costs to run on this machine is measured as
   the batch goes.

7. **Split across workers** (`selector/schedule.py`): the split that minimises
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
| `sources.json` | `build_sources.py` | Python source → tests that open it as a file |
| `funcs.json` | `build_map.py` | first and last line of every `def` |
| `warm.json` | `build_map.py` | code that already ran once the test modules are imported |
| `uses.bin`, `mentions.bin`, `classes.json`, `families.json` | `build_extra.py` | class → tests that instantiate or name it |
| `readers.bin`, `constants.json` | `build_extra.py` | constant → tests that execute a line reading it |
| `tcov.bin`, `tcov.json` | `trace_all.py` with `BR_TRACE_SPLIT=1`, `build_tcov.py` | (code object, line) → tests that execute it while the test itself runs, sqlglot being imported in full beforehand |
| `treaders.bin` | `build_extra.py` | constant → tests that read it while the test itself runs |
| `cases.bin`, `cases.json`, `casesrc.json` | `trace_cases.py`, `build_cases.py` | per fixture case, the lines it reaches |
| `setup.json` | `trace_setup.py` | what the executor tests' class setup executes and reads |
| `duckdb.bin` | `build_duckdb_answers.py` | SQL text → DuckDB's answer over the TPC tables |
| `ducksetup.json` | `build_duckdb_setup.py` | the statements each class setup sends to its DuckDB connection |
| `execmemo.bin`, `execphase.json` | `build_executor_memo.py` | optimized TPC query → executor result; the lines that produce it |
| `runcost.json` | `calibrate.py` | CPU seconds each test takes in this runner |

There is no table from a patch, or from a kind of edit, to failing tests: the
maps describe the pinned commit only, and every selected test was either
observed failing on the patched code or could not be ruled out.

## How it was checked

- `tools/gt_run.py` labels a patch the way the organisers did (apply to a fresh
  checkout, run the whole suite with pytest); it reproduces
  `sample/answers.json` exactly. `tools/mutate.py` generates patches of many
  kinds (operator and constant edits, fixture edits, comments, multi-file
  changes, test edits, deleted and swapped statements, class and module
  constants, docstrings, annotations, renames, new tests), which were labeled
  this way and used to check that the selector never leaves out a failing
  test. This is how a gap in the first coverage trace was found: with line
  events alone, CPython 3.13 does not report a function's first line, so
  one-line lambdas were never seen running. Those patches and labels are
  development data only; nothing derived from them is in the image.
- `tools/crosscheck_trace.py` compares the coverage tracer with `sys.settrace`
  line for line.
- `tools/test_pydiff.py` holds the unit checks for the code comparison,
  `tools/test_state.py` those for the comparison of import state;
  `tools/state_probe.py` shows what a given patch changes at import (for the
  unchanged tree: nothing). `tools/mutate_import.py` generates one-token
  patches to code that runs at import, labeled like the others.
- `tools/test_duckprobe.py` holds the unit checks for the DuckDB stand-in: what
  the caller sees and what really reaches DuckDB, in order, for every way a
  connection can leave the recorded setup.
- `tools/test_runner_paths.py` holds the unit checks for how a child finds
  patched files: redirected `open` and `io.open_code`, resolved paths that
  stay in the overlay, and modules imported on demand from patched text.
- `tools/trace_suite.py` traces the whole suite in one process, in collection
  order, and `tools/build_suite.py` compares it test by test with the runs
  alone. Of 2.59 million (test, line) pairs, 121 are executed in the suite but
  not alone: 16 lines, all in `Dialect._try_load`, where a dialect is
  registered again after another test emptied the registry. That function is
  one of those a change to which selects every test. (The other direction is
  large and harmless: alone, a test also runs the imports.)
- An audit of the selector itself: patches written to break one assumption
  each (where a comment lands, what a fixture edit shifts, what holds an old
  function, what reads a constant, what a test reads from disk, what only
  runs in suite order), each run through the selector and through pytest.
  The misses it found are the reason for the source-reader map, the
  redirects above, the rule for code that imports modules on demand or uses
  the registry they fill, the rebuilt map of who reads a constant, and the
  executor results being reused only when nothing `execute` runs outside the
  optimizer has changed.
- The runner was run on synthetic worst cases: infinite loops in tests, in
  imports and in class bodies, unbounded allocation, `os._exit`, stack
  overflow, and binary, renamed, deleted, new and non-applying files.

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
