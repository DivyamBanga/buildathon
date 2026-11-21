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
   to see the change. A function that binds other names afterwards (its local,
   cell or free names differ as sets) counts as changed in full, and so does any
   line whose instructions differ although its text does not.

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

   **A path in more than one section of a patch** (a file edited twice, deleted
   and created again, renamed and edited) selects every test. The sections are
   analysed one by one against the pinned file here, which is not what applying
   them in order gives.

   **Changed lines that speak to pytest**, in a file under `tests/` (the word
   itself, marks, fixtures, `xfail`, `__test__`, and the hooks only pytest calls
   on a unittest module or class: `setup_module`, `teardown_module`,
   `setup_class`, `setup_method`, `setup_function` and their `teardown_`
   twins), select every test of that file unchecked, and every test that
   imports it when it is a helper module. Added and removed lines count. The
   runner uses unittest, which ignores all of these, so running the test says
   nothing. (A new or changed `conftest.py` and the pytest configuration files
   select everything, as before.)

   **New tests and helpers in a test file.** A file that only gained whole new
   tests, test classes or helper functions selects nothing, but only if what
   was added can neither act nor be found by existing code. pytest runs the new
   test in the same process as every other, so a new or changed statement that
   may act outside its own locals, in a new or an edited test file, selects
   every test: an attribute or subscript store or `del` on anything but `self`
   (and on `self` anywhere but in a method that never rebinds it nor passes it
   on as another parameter), `global`, `nonlocal`, a read of `__dict__`,
   `__class__` and the like, and a call named like something that changes
   shared state or runs text (`setattr`, `update`, `clear`, `pop`, `append`,
   `exec`, `eval`, `reload`, `patch`, `start`, `register`, `setenv`, ...). The
   list of call names is a list, not a proof: a call into sqlglot that changes
   a shared table under another name is not found. A new
   module-level name must not be a builtin or a name the old module loads
   anywhere (existing code would find the new one); a new method must not be a
   name `unittest.TestCase` has, one the old module reads as an attribute or
   defines elsewhere, or the name of any test in the suite. Otherwise the file
   counts as changed, and its own tests run as for any edited test file.

   **A file that appears or vanishes** changes what its directory lists. The
   tests that listed that directory in their own run (`dirs.json`, from the
   `D` rows of the per-test traces: `os.listdir`, `os.scandir`, `glob`) are
   candidates; so is everything that could notice a new module, because which
   names the tests ask the import system for is not recorded: a new or removed
   `.py` file or directory counts for every test, a data file only for the
   tests that listed its directory. The import system's own listings are among
   the rows, so for `sqlglot/` and `tests/` that is nearly every test. A new
   test file under `tests/` keeps its own rule above. The candidates run in a
   clean child that holds the new file, except where the listing code reaches
   the checkout past the overlay (`sqlglotc/setup.py`) or the file is outside
   `sqlglot/` and `tests/`; those are selected unchecked.

   **A closer look at import-time changes** (`selector/state.py`). A module or
   class body runs once, at import, and all it can do is leave the package in
   another state: other module globals, class attributes, defaults or closures.
   When the candidates above would be expensive, a child first imports the
   whole package twice, pinned and patched, and compares the two results
   object by object (functions by their resolved instructions, defaults and
   closures; tables entry by entry; classes attribute by attribute). What comes
   out is the list of (module or class, attribute) pairs that differ, including
   whatever sqlglot itself derived from the changed value while importing.
   The comparison is also run, whatever the candidates cost, for a commit
   that needs a fresh import and has an executor test with recorded results
   (step 5) among its candidates: for instance an edit to the body of a parse
   function that sqlglot itself calls while it is imported, on a line only
   tests reach. Nothing is narrowed then, but only the comparison says that
   the fresh import left what the pinned one left, which is what reusing a
   recorded result needs.

   - No difference: the change cannot be observed (a flag only the type checker
     reads, a `not` on a value nothing uses), and it adds no candidate.
   - A function that kept its code but got another default or closure value is
     noticed only by calling it: the tests that run that function.
   - A data attribute of a class with a new value, when `areaders.bin` covers
     it: exactly the tests that read that attribute while they run, from
     tracing the pinned tests (`tools/trace_attrs.py`: every test alone, with
     each such attribute replaced by a descriptor that notes the read). A read
     through an instance or a subclass counts for the class that defines the
     attribute. This replaces the reach and the name of the next two rules for
     the pairs it covers; added and removed attributes, functions, enum
     members, anything with a `__get__` and any class the tracer could not
     name by module and qualified name stay with the rules below.
     The runner formats a subtest's message with `str()` and its parameters
     with `repr()` after each subtest, as pytest does when it reports one, so
     that the reads on that path are traced and a message that fails to format
     fails the test in a child too.
   - A constant with a new value is noticed only by reading it: the tests that
     can reach the module or class and execute a line reading that name.
   - Anything else on a class: the tests that can reach the class, as above.

   The same is done for a changed function that sqlglot itself runs while it
   is imported (a helper of a metaclass, the error formatter the parser calls
   while templates are parsed). Run alone, nearly every test "executes" such a
   function, because sqlglot is imported in its process. What the function did
   then is in the state of the package, so the candidates are the tests that
   run it themselves, plus whatever the comparison finds.

   Both imports also go through sqlglot's on-demand loading, on dialects that
   are not imported yet (`Dialect.get`, `Dialect[...]`, `Dialect.get_or_raise`,
   attribute access on the lazy packages), and what comes back is compared
   too: importing every module directly would not notice a patch that breaks
   that machinery.

   "Run" and "read" here mean while the test itself runs. A second line map
   (`tcov.bin`) records each test with sqlglot imported in full beforehand, so
   that what sqlglot executes while importing itself, whichever test runs, is
   not mistaken for the test using it: that part is exactly what the state
   comparison accounts for. The comparison says "different" for anything it
   does not know how to compare, and the candidates fall back to the rule
   above when it is inconclusive (too many differences, state outside the
   package changed: the environment, logging, warning filters, the recursion
   limit, or any builtin). A module that binds a `__builtins__` of its own
   counts as a difference every test importing it can see: the names its code
   leaves to the builtins may mean something else. When a module no longer imports at all, every test that
   imports it or a module that depends on it is a candidate, whether or not it
   could "see" the changed class: the comparison is therefore always run
   before tests are left out for that reason.

   What the comparison takes for granted, each checked against the pinned
   code: annotations, docstrings and what `typing` records about a TypedDict
   are not read while tests run; a default or a closure value of a function
   matters only when the function is called. The order of the entries of a
   table (a dict, at any depth below an attribute of a module or class, also
   inside a function's defaults and closure, a partial or an instance held
   there) and of a tuple or list of classes is compared where it is fixed: `maps/order.json`
   lists the attributes whose tables come out in the same order in every fresh
   import of the pinned package (`tools/build_order.py`: 16 imports, each with
   its own hash seed and memory layout), and for those another order is a
   difference (`arg_types` is read in order to give positional arguments their
   names). Only for the attributes that are not listed, whose order already
   differs from run to run because sqlglot builds the table by looping over a
   set, is the order taken as irrelevant, and so is the order of a set. The order of
   the attributes of an instance (its `__dict__` itself) is not compared. A function that imports modules on demand (four in
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
     the functions already in memory and runs the tests at once. A function
     whose signature changed (parameter names, number, order or kind, whether
     it is a generator or a coroutine, its free variables, its name) is not
     swapped: a decorator or a registry reads those when `def` runs and keeps
     what it found, so such a commit gets a clean import and the comparison of
     import state, whose differences name the tests that can see what the
     definition made.
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
     the query, plans it and runs the plan. The result of the last two steps
     depends on three things: the optimized query, the tables, and the code
     that plans and executes (with whatever that code reads). When all three
     are what they were at the pinned commit, the recorded result is returned
     and the query is not planned and executed again. Each is checked, not
     assumed:
     - *The query*: digests taken from the tree itself, with no sqlglot code
       involved (`runner.tree_digests`). The key covers every node's class,
       arguments in order, the type of every leaf value, and the wiring (a
       node held in two places, a child that names another parent or
       position). What the optimizer leaves on a node besides its arguments
       sits in three slots: the type annotation, the comments, and the meta
       dictionary (token positions). Each slot has a digest of its own. A
       patch to the tokenizer, the parser or the type annotator can change
       those slots on every tree without changing one argument, and planning
       and executing a query hardly ever looks at them. Whether it does is
       recorded per query (`tools/trace_slots.py`: a descriptor in each
       slot's place notes every read of a node of the optimized tree, or of a
       copy of one, between planning and the end of execution; copying a slot
       along with its node is not a read): no query reads meta, 15 of 57 read
       comments, one asks a node for its type. For a slot a query reads, the
       slot's digest must be the recorded one too. So that a copy cannot
       behave differently either, a slot of an unusual shape stays in the
       key (comments that are not a list of strings, meta that is not a flat
       dictionary of plain values, an annotation that is the node itself or
       holds something that is not an expression). A node also caches its
       hash, and the planner hashes nodes. A cached hash is a number of one
       process, so no digest can cover it; it cannot be seen as long as it
       is the true one. A patched optimizer can leave a node whose cached
       hash predates a change below it, in a tree that is otherwise the
       pinned one (found with a one-token edit of `simplify`). So before a
       recorded result is used, every cached hash in the tree is compared
       with a fresh one computed by the node's own class, and put back; one
       stale hash and the query is executed (`runner.hashes_fresh`).
     - *The tables*: what `ensure_tables` returned for this call must hold
       exactly the pinned tables, by name and by content, at that moment (a
       table changed in place is not the pinned table any more).
     - *The code and what it reads*: the patch changes none of the lines
       `execute` runs outside the optimizer (`execphase.json`). After a hot
       swap nothing else can differ. After a clean import the comparison of
       import state (step 3) lists every module or class attribute that
       differs, and each must be out of that code's sight: a function with
       other defaults or closure must not be one it runs; a function whose
       code alone differs, and which the patch itself edits, counts through
       its changed lines exactly as after a hot swap (any other function with
       other code must not be one it runs); a plain data
       attribute of a class must not be one it read (`execattrs.json`:
       recorded with a descriptor in the attribute's place that notes every
       read through the class, a subclass or an instance; when the attribute
       holds a table, a dict that differs in some entries only, the query
       must have walked the table or looked one of those entries up:
       `tools/recdict.py` puts a recording dict in the table's place, a
       lookup and an entry are the same key when `state.key_names` gives them
       a common name, and what a query looked up counts only if it ran the
       same lines with and without the recording dicts); any other constant
       must not be named by a statement it runs (`execnames.json`: a name or
       attribute written in the code is read by the statement it stands in;
       a name that only occurs in a string, an import or a `global` line
       counts for its whole function); any other name of a module with
       another value (a function replaced by a changed decorator, an object
       made at import) is read the same way, by the statements that name it,
       the names Python itself asks a module for (two underscores) excepted;
       a name in capitals that appears or
       vanishes (a constant renamed at its definition) is seen the same way,
       by the statements that name it, as long as the lines of that code
       that look a name up by a computed text or walk a namespace are the
       ones reviewed at the pinned commit (the generator's `dir(cls)` for
       names ending in `_sql`; `eval` of generated code in a dict built from
       the executor's function table); that function table itself (`ENV` in
       `executor/env.py`) is copied by every query, and a query uses of it
       only the entries its code names, so a table that differs in some
       entries concerns the queries whose evaluated code names one of them
       (all names of every piece of code a query evaluates are recorded,
       and the key of the one lookup by a computed name; used only while the
       lines of the package that mention the table or a copy of it are the
       ones this was read off);
       anything else (other added or removed attributes, other bases, a
       difference on the classes the recorded results are made of) turns the
       reuse off, as does an inconclusive comparison or a patch outside
       `sqlglot/`. A patched module that is imported on demand is imported
       from its new source when a test first asks for it; that leaves the
       state the pinned module would leave only when function bodies alone
       changed and none of them runs while sqlglot is imported
       (`importrun.json`). Then it counts as a hot swap; otherwise the reuse
       is off.
     - *Query by query*: when the patch does change lines of that phase, or
       attributes it reads, the record of each single query says whether that
       query is concerned (`execqueries.bin`: the lines it runs and the
       attributes it reads when planned and executed alone in a fresh
       process). A query that touches nothing changed keeps its recorded
       result; the others are executed.
   - The executor tests' class setup parses every TPC table from CSV. For a
     patch that touches nothing that setup runs, the tables are built once and
     shared by the children. A child that imports the patched tree afresh
     cannot take the tables (their class may have changed), but for a patch
     that touches no file outside `sqlglot/` the rows are what they were:
     the files and the test code that parses them are untouched. Such a child
     skips the parsing only and builds every table with the patched class
     from the rows parsed at the pinned commit.

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
| `order.json` | `build_order.py` | module or class attribute → its tables come out in the same order in every fresh import |
| `tcov.bin`, `tcov.json` | `trace_all.py` with `BR_TRACE_SPLIT=1`, `build_tcov.py` | (code object, line) → tests that execute it while the test itself runs, sqlglot being imported in full beforehand |
| `treaders.bin` | `build_extra.py` | constant → tests that read it while the test itself runs |
| `cases.bin`, `cases.json`, `casesrc.json` | `trace_cases.py`, `build_cases.py` | per fixture case, the lines it reaches |
| `setup.json` | `trace_setup.py` | what the executor tests' class setup executes and reads |
| `duckdb.bin` | `build_duckdb_answers.py` | SQL text → DuckDB's answer over the TPC tables |
| `ducksetup.json` | `build_duckdb_setup.py` | the statements each class setup sends to its DuckDB connection |
| `areaders.bin` | `trace_attrs.py`, `build_areaders.py` | `module:Class\|attribute` → tests that read that class attribute while the test itself runs |
| `execmemo.bin`, `execphase.json`, `execattrs.json`, `execqueries.bin` | `build_executor_memo.py` | (tables, optimized TPC query) → executor result; the lines that produce it; the class attributes they read; the same two per query |
| `execnames.json` | `build_execnames.py` | constant names written in the functions those lines belong to |
| `importrun.json` | `build_importrun.py` | code objects that run while the whole package is imported |
| `dirs.json` | `trace_all.py`, `build_dirs.py` | directory → tests that listed it (the `D` rows of the traces) |
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
- `tools/test_kinds.py` holds the unit checks for the rules about kinds of
  change that one test run cannot settle (a path in several sections, changed
  lines that speak to pytest, new tests and helpers that store outside their
  locals or shadow a name, a file that appears or vanishes) and replays the
  patches in `tools/kinds_seeds/`: each was a commit for which pytest failed a
  test and the selector selected nothing of the kind.
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
