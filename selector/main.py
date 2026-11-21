"""Blast Radius selector: one JSON line per commit on stdout, logs on stderr.

For every patch:
  1. analyze.py names the tests that could notice the change (dependency map).
  2. Those tests are run against the patched code in forked children
     (runner.py) and the ones that fail are selected. A test is selected
     without being run when running it would cost more CPU than selecting it.
  3. schedule.py splits the selection across workers.

Whatever goes wrong, every commit still gets a line: an unverified candidate
is selected, and a patch that cannot be analysed selects every test.
"""

from __future__ import annotations

import gc
import glob
import json
import os
import select
import shutil
import signal
import sys
import time
import types

from . import runner, schedule, state
from .analyze import Plan, analyze
from .covmap import CovMap

ROOT = os.environ.get("BR_SQLGLOT", "/sqlglot")
DATA = os.environ.get("BR_DATA", "/data")
MAPS = os.environ.get(
    "BR_MAPS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps")
)
TMP = os.environ.get("BR_TMP", "/tmp/br")

# The whole batch has 180 s of wall clock. Past this, whatever is still being
# checked is selected unchecked.
HARD_DEADLINE = float(os.environ.get("BR_DEADLINE", "135"))
MAX_PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
# One child at a time costs the least CPU: two busy processes slow each other
# down, and CPU time is what is scored. A second child runs alongside only once
# the batch is this many seconds in, so that a slow batch still finishes in time.
SEQUENTIAL_UNTIL = float(os.environ.get("BR_SEQUENTIAL", "75"))
# First guess at the CPU seconds the scoring machine needs per recorded second
# of a plain test (the recorded seconds came from a machine of the same kind).
MACHINE = float(os.environ.get("BR_MACHINE", "1.0"))
# Running a test is worth it when its CPU cost is under this share of what
# selecting it would add. A test that fails stops early and costs little, so
# the share stays high while nothing has failed on the commit (the change may
# be harmless and the whole selection avoidable) and drops once something has.
PASS_QUIET = float(os.environ.get("BR_PASS_QUIET", "0.9"))
PASS_LIVE = float(os.environ.get("BR_PASS_LIVE", "0.5"))
# Tests cheaper than this to check are weighed together, not one by one.
CHEAP = 0.5
# A check dearer than PROBE_ABOVE seconds is preceded by cheap probes worth up
# to PROBE_SHARE of its cost.
PROBE_ABOVE = 3.0
PROBE_SHARE = 0.15
SETUP_ALLOWANCE = 40.0
# CPU seconds a child may burn between tests (imports, class setup) before it
# counts as stuck, and how far any of its processes may grow past this one.
SETUP_CPU = 8.0
# The largest test (test_max_nodes) peaks about 1.7 GiB above the process it is
# forked from; the container has 4 GiB and children run one at a time at first.
MEMORY_ALLOWANCE = 2.4 * 1024**3
# A change to code that runs at import is first looked at by what it leaves
# behind (selector/state.py), when running or selecting its candidates would
# cost more than a few times what that look costs (in seconds of a plain test).
STATE_PROBE = os.environ.get("BR_STATE_PROBE", "1") != "0"
PROBE_COST = 0.8
VERBOSE = os.environ.get("BR_VERBOSE", "") != ""
# For experiments only: pretend every CPU second a child uses costs this many,
# to see what the selector decides on a slower machine than the one at hand.
SLOWDOWN = float(os.environ.get("BR_SLOWDOWN", "1"))
# For experiments only: run every candidate, whatever it costs, so that the
# simulated time is what the failing tests alone come to.
ALWAYS_CHECK = os.environ.get("BR_ALWAYS_CHECK", "") != ""
TRACE = os.environ.get("BR_TRACE", "") != ""
FIXTURES = "tests/fixtures/"
EXECUTOR_CLASS = "tests/test_executor.py::TestExecutor"


def log(*parts) -> None:
    print(*parts, file=sys.stderr, flush=True)


_TICKS = float(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100.0


_PAGE = float(os.sysconf("SC_PAGE_SIZE")) if hasattr(os, "sysconf") else 4096.0


def tree_usage(pid: int) -> tuple[float, float]:
    """(CPU seconds used so far, largest resident size in bytes) over a process and its descendants.

    The CPU figure includes descendants that were already reaped.
    """
    total = 0.0
    largest = 0.0
    pending = [pid]
    while pending:
        current = pending.pop()
        try:
            with open(f"/proc/{current}/stat", "rb") as handle:
                data = handle.read()
            # Fields after the command name: state, ppid, ..., utime, stime, cutime, cstime, ..., rss.
            fields = data[data.rindex(b")") + 2:].split()
            total += (int(fields[11]) + int(fields[12]) + int(fields[13]) + int(fields[14])) / _TICKS
            largest = max(largest, int(fields[21]) * _PAGE)
            with open(f"/proc/{current}/task/{current}/children", "rb") as handle:
                pending.extend(int(child) for child in handle.read().split())
        except (OSError, ValueError, IndexError):
            pass
    return total, largest


def tree_cpu(pid: int) -> float:
    return tree_usage(pid)[0]


class Job:
    def __init__(self, plan: Plan, mode: str, pending: list[int], blind: set[int]) -> None:
        self.plan = plan
        self.mode = mode
        self.pending = pending  # map bits still to run
        self.blind = blind  # selected without running
        self.failed: set[int] = set()
        self.estimates: dict[int, float] = {}
        self.slices: dict = {}  # (bit, fixture) -> case numbers worth running
        # representative test -> tests of its class that fail exactly when it does
        self.followers: dict[int, list[int]] = {}
        self.strikes = 0
        self.timeouts = 0
        self.spawned = 0
        self.estimate = 0.0
        self.replanned = False
        self.candidates: list[int] = []  # every test still to be decided
        self.everything: set[int] = set()  # every candidate, followers included
        self.decided = False
        # Test files of non-candidates that import a changed module: the child
        # imports them first, and a file that fails takes all its tests with it.
        self.import_files: list[str] = []
        self.setup_untouched: bool | None = None
        self.memo_ok: bool | None = None
        self.deadline = float("inf")  # wall-clock limit for checking this commit
        self.probed = False  # the state left by its import-time changes has been compared


class Run:
    def __init__(self, pid: int, job: Job, tests: list[int], now: float) -> None:
        self.pid = pid
        self.job = job
        self.tests = tests
        self.done: set[int] = set()
        self.current: int | None = None
        self.current_started = now
        self.last_event = now
        self.buffer = b""
        self.finished = False
        self.fatal: str | None = None
        self.started = now
        self.stopped = False  # the coordinator chose to end it
        self.overlay = ""
        self.current_cpu = 0.0  # CPU the child's process tree had used when the current test began
        self.probe = False  # compares import state instead of running tests
        self.state: dict | None = None  # what it found
        self.cpu_checked = now


class Coordinator:
    def __init__(self) -> None:
        self.start = time.monotonic()
        tests = json.load(open(os.path.join(DATA, "tests.json"), encoding="utf-8"))["tests"]
        self.numbers = [test["number"] for test in tests]
        self.ms = {test["number"]: int(test["seconds"] * 1000 + 0.5) for test in tests}
        self.covmap = CovMap(MAPS)
        number_of = {test["id"]: test["number"] for test in tests}
        self.bit_number = [number_of.get(test_id, -1) for test_id in self.covmap.ids]
        self.bit_seconds = [
            self.ms[number] / 1000.0 if number >= 0 else 0.0 for number in self.bit_number
        ]
        self.bit_class = [test_id.rsplit("::", 1)[0] for test_id in self.covmap.ids]
        # What a test costs to run here, measured with this runner; the recorded
        # seconds stand in for a test that was never measured.
        self.bit_cost = list(self.bit_seconds)
        self.class_setup: dict[str, float] = {}
        # The executor tests' costs when their class setup is reused (see runner).
        self.fast_cost: dict[int, float] = {}
        self.fast_setup: dict[str, float] = {}
        # ... and when, on top of that, recorded executor results can be reused.
        self.memo_cost: dict[int, float] = {}
        self.exec_phase: dict | None = None
        # ... and when they run their own setup but DuckDB's recorded answers are
        # reused (a clean import, or a patch that touches what the setup runs).
        self.answer_cost: dict[int, float] = {}
        self.duckdb_ready: bool | None = None
        try:
            runcost = json.load(open(os.path.join(MAPS, "runcost.json")))
            measured = runcost["tests"][: len(self.bit_cost)]
            # Rescale to recorded seconds using the plain single-process dialect
            # tests, so the speed of the machine it was measured on drops out.
            plain = [
                bit
                for bit, value in enumerate(measured)
                if value and self.covmap.ids[bit].startswith("tests/dialects/")
            ]
            scale = sum(self.bit_seconds[bit] for bit in plain) / sum(measured[bit] for bit in plain)
            for bit, value in enumerate(measured):
                if value is not None:
                    self.bit_cost[bit] = value * scale
            self.class_setup = {name: value * scale for name, value in runcost.get("setup", {}).items()}
            fast = runcost.get("fast", {})
            self.fast_cost = {int(bit): value * scale for bit, value in fast.get("tests", {}).items()}
            self.fast_setup = {name: value * scale for name, value in fast.get("setup", {}).items()}
            self.memo_cost = {
                int(bit): value * scale for bit, value in runcost.get("memo", {}).get("tests", {}).items()
            }
            self.answer_cost = {
                int(bit): value * scale for bit, value in runcost.get("answers", {}).get("tests", {}).items()
            }
        except (OSError, ValueError, KeyError, ZeroDivisionError):
            pass
        # CPU seconds this machine needs per recorded second of a plain test. It
        # starts at the configured guess and follows what the children really use.
        self.machine = MACHINE
        self.observed_cpu = 0.0
        self.observed_base = 0.0
        mapped = set(self.covmap.ids)
        # Tests the map has never seen cannot be reasoned about: always selected.
        self.unmapped = [test["number"] for test in tests if test["id"] not in mapped]
        self.test_files = list(dict.fromkeys(test["file"] for test in tests))
        self.decisions: dict[str, list[int]] = {}
        self.queue: list[Job] = []
        self.running: dict[int, Run] = {}
        self.function_index: dict = {}
        self.sequence = 0
        self.ran_seconds = 0.0
        self._opens: dict[int, list[str]] = {}
        self.setup_map: dict | None = None
        self.resident = 0.0  # this process's own resident size, the baseline for its forks

    # ---- deciding -------------------------------------------------------

    def cost(self, bits) -> float:
        return schedule.cost_ms([self.ms[self.bit_number[bit]] for bit in bits]) / 1000.0

    def opens_of(self, bit: int) -> list[str]:
        found = self._opens.get(bit)
        if found is None:
            found = self._opens[bit] = self.covmap.opens_of(bit)
        return found

    def changed_fixtures(self, plan: Plan) -> set[str]:
        return {
            path[len(ROOT) + 1:][len(FIXTURES):]
            for path in plan.redirects
            if path[len(ROOT) + 1:].startswith(FIXTURES)
        }

    def followers(self, plan: Plan, bits: list[int], job: Job) -> list[int]:
        """Drop tests that only share a class-level fixture load with a representative.

        When a patch edits nothing but fixture files, a test that reads the file
        but runs the same code whatever its cases are (the map knows which) can
        only fail if loading the file fails, and then every such test of the
        class fails with it. One of them is run for the lot.
        """
        changed = self.changed_fixtures(plan)
        if not plan.sliceable or plan.units or not changed:
            return bits
        groups: dict[str, list[int]] = {}
        for bit in bits:
            relevant = [fixture for fixture in self.opens_of(bit) if fixture in changed]
            if relevant and all(self.covmap.case_independent(bit, fixture) for fixture in relevant):
                groups.setdefault(self.bit_class[bit], []).append(bit)
        dropped: set[int] = set()
        for members in groups.values():
            if len(members) > 1:
                lead = min(members, key=lambda bit: (self.bit_cost[bit], bit))
                job.followers[lead] = [bit for bit in members if bit != lead]
                dropped.update(job.followers[lead])
        return [bit for bit in bits if bit not in dropped]

    def units_of(self, plan: Plan, bit: int) -> dict:
        """The changed code this one test executes (by the whole-test map)."""
        mask = 1 << bit
        mine: dict = {}
        for path, entries in plan.units.items():
            for first, lines in entries:
                if lines is None:
                    ran = self.covmap.code_any(path, first)
                else:
                    ran = self.covmap.code_lines(path, first, lines)
                if ran & mask:
                    mine.setdefault(path, []).append((first, lines))
        return mine

    def estimate(self, plan: Plan, bits: list[int], job: Job) -> None:
        """What checking each candidate should cost, and which fixture cases it needs."""
        changed = self.changed_fixtures(plan)
        for bit in bits:
            share = 1.0
            mine = self.units_of(plan, bit) if plan.units else {}
            if plan.sliceable:
                shares = []
                # Only .sql fixtures hold cases; the other files a test opens are plain data.
                loops_over = [name for name in self.opens_of(bit) if name.endswith(".sql")]
                for fixture in dict.fromkeys(loops_over + self.covmap.sliced_fixtures(bit)):
                    info = self.covmap.case_info(bit, fixture)
                    if info is None:
                        if not self.covmap.case_independent(bit, fixture):
                            shares.append(1.0)
                        continue
                    # When the test runs none of the changed code, only the cases
                    # the patch edits can differ.
                    keep = self.covmap.case_slice(bit, fixture, mine) if mine else set()
                    if keep is None:
                        shares.append(1.0)
                        continue
                    job.slices[(bit, fixture)] = keep
                    weight = info["weight"]
                    part = sum(weight[index] for index in keep if index < len(weight))
                    if fixture in changed:
                        part += 2.0 / max(1, info["count"])
                    shares.append(min(1.0, part))
                if shares:
                    share = max(0.03, max(shares))
            # In recorded seconds of a plain test; `self.machine` turns it into CPU here.
            job.estimates[bit] = self.full_cost(job, bit) * share

    def weigh(self, bits: list[int], estimates: dict, chosen: list[int], passing: float):
        """Split `bits` into (worth checking, selected unchecked), given `chosen` is selected anyway.

        Checking a test is worth it when its CPU cost is below what selecting
        it would add to the commit, times the chance that it passes.
        """
        if ALWAYS_CHECK:
            return list(bits), []
        chosen = list(chosen)
        check: list[int] = []
        blind: list[int] = []
        scale = self.machine
        big = sorted((bit for bit in bits if estimates[bit] * scale > CHEAP), key=estimates.get, reverse=True)
        cheap = [bit for bit in bits if estimates[bit] * scale <= CHEAP]
        base = self.cost(chosen)
        for bit in big:
            with_bit = self.cost(chosen + [bit])
            if estimates[bit] * scale < passing * (with_bit - base):
                check.append(bit)
            else:
                blind.append(bit)
                chosen.append(bit)
                base = with_bit
        if cheap:
            marginal = self.cost(chosen + cheap) - base
            if sum(estimates[bit] for bit in cheap) * scale < passing * marginal:
                check.extend(cheap)
            else:
                blind.extend(cheap)
        if TRACE:
            for bit in big:
                log(
                    f"    weigh {self.covmap.ids[bit].split('::')[-1]}: check costs "
                    f"{estimates[bit] * scale:.2f}s, recorded {self.bit_seconds[bit]:.2f}s -> "
                    + ("check" if bit in check else "select")
                )
            log(
                f"    weigh {len(cheap)} cheap tests: check costs "
                f"{sum(estimates[bit] for bit in cheap) * scale:.2f}s -> "
                + ("check" if cheap and cheap[0] in check else "select")
            )
        return check, blind

    def order(self, bits: list[int], estimates: dict) -> list[int]:
        """Cheapest classes first, so a failure shows up early; a class stays together."""
        totals: dict[str, float] = {}
        for bit in bits:
            totals[self.bit_class[bit]] = totals.get(self.bit_class[bit], 0.0) + estimates[bit]
        return sorted(
            bits, key=lambda bit: (totals[self.bit_class[bit]], self.bit_class[bit], estimates[bit], bit)
        )

    def plan_commit(self, plan: Plan) -> None:
        if plan.select_all:
            self.decisions[plan.commit] = list(self.numbers)
            return
        bits = [
            bit
            for bit in self.covmap.bits(plan.candidates & ~plan.unverified)
            if self.bit_number[bit] >= 0
        ]
        importers = [
            bit
            for bit in self.covmap.bits(plan.import_check & ~plan.candidates)
            if self.bit_number[bit] >= 0
        ]
        if not bits and not importers:
            self.finish(plan, set())
            return
        mode = "clean" if plan.needs_clean else "hot"
        job = Job(plan, mode, [], set())
        job.import_files = sorted({self.covmap.ids[bit].split("::", 1)[0] for bit in importers})
        job.everything = set(bits)
        job.candidates = self.followers(plan, bits, job)
        self.estimate(plan, job.candidates, job)
        # Cheap commits go first: by the time a close call comes up, the real
        # cost of running tests on this machine has been measured.
        job.estimate = sum(job.estimates.values())
        self.queue.append(job)

    def overhead(self, job: Job, bits) -> float:
        """Base seconds a child spends outside the tests themselves."""
        classes = {self.bit_class[bit] for bit in bits}
        setup = 0.0
        for name in classes:
            if name == EXECUTOR_CLASS and self.executor_setup_untouched(job):
                setup += self.fast_setup.get(name, 0.15)  # its tables come ready-made
            else:
                setup += self.class_setup.get(name, 0.0)
        return setup + (0.45 if job.mode == "clean" else 0.05)

    def decide(self, job: Job) -> bool:
        """Choose what to run for a commit. False when nothing is worth running."""
        job.decided = True
        plan = job.plan
        bits = job.candidates
        check, blind = self.weigh(bits, job.estimates, [], PASS_QUIET)
        total = self.machine * (self.overhead(job, check) + sum(job.estimates[bit] for bit in check))
        saving = self.cost(bits) - self.cost(blind)
        if not check or (total >= saving and not ALWAYS_CHECK):
            plan.note(f"selected unchecked: checking {total:.1f}s vs saving {saving:.1f}s")
            if job.import_files:
                # Nothing to run, but the files that only import the change must still load.
                job.pending = []
                job.blind = set(bits)
                return True
            self.finish(plan, job.everything)
            return False
        # Before paying for an expensive check, find out cheaply whether the change
        # is real: run a few of the cheapest tests that were going to be selected
        # anyway. If one fails, the expensive tests are weighed again as likely to fail.
        costly = 0.0
        for bit in check:
            # What the check may come to if the change turns out to be real.
            potential = job.estimates[bit] * self.worst_cost(job, bit) / max(self.full_cost(job, bit), 1e-9)
            if potential * self.machine > PROBE_ABOVE:
                costly += potential
        if costly and blind:
            budget = max(0.5 / self.machine, PROBE_SHARE * costly)
            probes = []
            for bit in sorted(blind, key=lambda bit: (job.estimates[bit], bit)):
                if job.estimates[bit] > budget:
                    break
                budget -= job.estimates[bit]
                probes.append(bit)
            if probes:
                moved = set(probes)
                blind = [bit for bit in blind if bit not in moved]
                check = check + probes
                plan.note(f"{len(probes)} cheap tests run first as probes")
        plan.note(f"checking {len(check)} (~{total:.1f}s), {len(blind)} selected unchecked")
        job.pending = self.order(check, job.estimates)
        job.blind = set(blind)
        # A change that makes every test crawl must not eat the whole batch's time.
        job.deadline = time.monotonic() + max(20.0, 4.0 * total)
        return True

    def replan(self, run: Run) -> bool:
        """Something failed, so the change is real: recheck what is still worth running.

        Returns True when the child should be stopped.
        """
        job = run.job
        job.replanned = True
        remaining = [bit for bit in run.tests if bit not in run.done]
        if not remaining:
            return False
        if self.memo_cost and self.executor_memo_ok(job):
            # A real change usually alters some optimized queries, and those are
            # executed in full: expect half way between the best and the worst.
            for bit in remaining:
                if bit in self.memo_cost:
                    ratio = self.worst_cost(job, bit) / max(self.full_cost(job, bit), 1e-9)
                    job.estimates[bit] *= (1.0 + ratio) / 2.0
        check, blind = self.weigh(remaining, job.estimates, list(job.blind | job.failed), PASS_LIVE)
        if not blind:
            return False
        job.blind.update(blind)
        keep = set(check)
        job.pending = [bit for bit in remaining if bit in keep]
        run.stopped = True
        return True

    def finish(self, plan: Plan, bits: set[int]) -> None:
        bits = set(bits).union(self.covmap.bits(plan.unverified))
        numbers = sorted(self.bit_number[bit] for bit in bits if self.bit_number[bit] >= 0)
        if plan.candidates:
            numbers = sorted(set(numbers).union(self.unmapped))
        self.decisions[plan.commit] = numbers

    def finish_job(self, job: Job, unchecked=()) -> None:
        if not job.decided:
            # Never got as far as choosing what to run: every candidate is selected.
            self.finish(job.plan, job.everything)
            return
        selected = job.blind | job.failed | set(unchecked)
        for lead, members in job.followers.items():
            if lead in selected:
                selected.update(members)
        self.finish(job.plan, selected)

    # ---- running --------------------------------------------------------

    def warm_up(self) -> None:
        os.environ["SKIP_INTEGRATION"] = "0"
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        sys.dont_write_bytecode = True
        runner.warm_up(ROOT, self.test_files)
        if any(job.plan.swaps for job in self.queue):
            prefix = ROOT.rstrip("/") + "/"
            function = types.FunctionType
            index: dict = {}
            for item in gc.get_objects():
                if type(item) is function:
                    filename = item.__code__.co_filename
                    if filename.startswith(prefix):
                        index.setdefault(filename, []).append(item)
            self.function_index = index
        gc.collect()
        gc.freeze()

    def case_filter(self, job: Job, tests: list[int]) -> dict:
        """One filter per fixture for the whole child: the union of what its tests need."""
        if not job.slices:
            return {}
        inside = set(tests)
        by_fixture: dict[str, list] = {}
        for (bit, fixture), keep in job.slices.items():
            if bit in inside:
                by_fixture.setdefault(fixture, []).append((bit, keep))
        cases = {}
        for fixture, entries in by_fixture.items():
            sliced = {bit for bit, _ in entries}
            # Every other test here that reads the fixture must not care about its cases.
            if any(
                bit not in sliced
                and fixture in self.opens_of(bit)
                and not self.covmap.case_independent(bit, fixture)
                for bit in tests
            ):
                continue
            allowed: set[int] = set()
            kinds = set()
            for bit, keep in entries:
                allowed |= keep
                kinds.add(self.covmap.case_info(bit, fixture)["kind"])
            for kind in kinds:
                cases[(kind, fixture)] = frozenset(allowed)
        return cases

    @staticmethod
    def touches(plan: Plan, ran: dict) -> bool:
        """Whether the patch changes any of the lines in `ran` (path -> [[code firstlineno, line]])."""
        for path, entries in plan.units.items():
            rows = ran.get(path)
            if not rows:
                continue
            for first, lines in entries:
                if any(first == ran_first and (lines is None or line in lines) for ran_first, line in rows):
                    return True
        return False

    def executor_setup_untouched(self, job: Job) -> bool:
        """True when the patch touches nothing the class setup of the executor tests executes or reads."""
        if job.setup_untouched is None:
            job.setup_untouched = self._executor_setup_untouched(job)
        return job.setup_untouched

    def _executor_setup_untouched(self, job: Job) -> bool:
        if job.mode != "hot" or not job.plan.sliceable:
            return False
        if self.setup_map is None:
            try:
                self.setup_map = json.load(open(os.path.join(MAPS, "setup.json")))
            except (OSError, ValueError):
                self.setup_map = {}
        setup = self.setup_map.get(EXECUTOR_CLASS)
        if not setup or self.touches(job.plan, setup["units"]):
            return False
        # The .sql fixtures are read again by the child itself; the data files are not.
        changed = {path[len(ROOT) + 1:] for path in job.plan.redirects}
        return not any(path in changed and not path.endswith(".sql") for path in setup["opens"])

    def executor_memo_ok(self, job: Job) -> bool:
        """True when recorded executor results may stand in for unchanged TPC queries:
        the patch changes nothing that runs between the optimized form of a query and its result."""
        if job.memo_ok is None:
            job.memo_ok = False
            # Not for a patched module that is imported on demand: its class and
            # module bodies are not lines the recorded phase could have executed,
            # yet what they define can be read there.
            if self.memo_cost and not job.plan.lazy and self.executor_setup_untouched(job):
                if self.exec_phase is None:
                    try:
                        self.exec_phase = json.load(open(os.path.join(MAPS, "execphase.json")))
                    except (OSError, ValueError):
                        self.exec_phase = {}
                job.memo_ok = bool(self.exec_phase) and not self.touches(job.plan, self.exec_phase)
        return job.memo_ok

    def worst_cost(self, job: Job, bit: int) -> float:
        """The same when no recorded executor result can be reused (every query changed)."""
        if bit in self.fast_cost and self.executor_setup_untouched(job):
            return self.fast_cost[bit]
        return self.bit_cost[bit]

    def full_cost(self, job: Job, bit: int) -> float:
        """What the whole test costs to run for this commit, in recorded seconds of a plain test."""
        if bit in self.memo_cost and self.executor_memo_ok(job):
            return self.memo_cost[bit]
        if bit in self.fast_cost and self.executor_setup_untouched(job):
            return self.fast_cost[bit]
        if bit in self.answer_cost and self.duckdb_answers_expected(job):
            return self.answer_cost[bit]
        return self.bit_cost[bit]

    def duckdb_record(self) -> bool:
        """Load DuckDB's recorded answers and the setup statements they belong to."""
        if self.duckdb_ready is None:
            if runner.DUCKDB_ANSWERS is None:
                runner.load_duckdb_answers(os.path.join(MAPS, "duckdb.bin"))
            runner.load_duckdb_setups(os.path.join(MAPS, "ducksetup.json"))
            self.duckdb_ready = runner.prepare_duck_probe()
        return self.duckdb_ready

    def duckdb_answers_expected(self, job: Job) -> bool:
        """For the estimate only: will the child's own setup be recognised as the pinned one?

        The child finds out for itself (runner._ProbingConnection); here the
        answer is no when the patch edits the executor tests, their helpers or a
        file the setup hands to DuckDB.
        """
        if not self.answer_cost or not self.duckdb_record() or not runner.DUCKDB_ANSWERS:
            return False
        touched = set()
        for patch in job.plan.files:
            touched.update(path for path in (patch.path, patch.old_path) if path)
        if "tests/test_executor.py" in touched or "tests/helpers.py" in touched:
            return False
        return not any(path in touched for setup in runner.DUCKDB_SETUPS.values() for path in setup.get("files", ()))

    def executor_tables(self, job: Job, tests: list[int]) -> bool:
        """Whether the child may take the executor tests' tables ready-made.

        Then the tables built here at the pinned commit are exactly what the
        child's own class setup would build.
        """
        if not any(self.bit_class[bit] == EXECUTOR_CLASS for bit in tests):
            return False
        if not self.executor_setup_untouched(job):
            return False
        if runner.EXECUTOR_TABLES is None:
            try:
                runner.build_executor_tables()
                runner.load_duckdb_answers(os.path.join(MAPS, "duckdb.bin"))
                runner.load_executor_memo(os.path.join(MAPS, "execmemo.bin"))
                gc.freeze()
            except Exception as error:
                log(f"could not prebuild the executor tables: {error!r}")
                self.setup_map[EXECUTOR_CLASS] = None
                return False
        return True

    def wants_probe(self, job: Job) -> bool:
        """Whether to compare import state before deciding what to run for the commit."""
        plan = job.plan
        if not STATE_PROBE or job.probed or job.mode != "clean":
            return False
        if not plan.import_candidates or not plan.probe_ok or not self.covmap.has_test_time():
            return False
        if plan.narrowed:
            # Tests were left out as unable to see the change to a class or
            # module body. That is only true if the module still imports, and
            # importing it is what the comparison does first.
            return True
        bits = job.candidates
        checking = self.machine * (self.overhead(job, bits) + sum(job.estimates[bit] for bit in bits))
        now = min(checking, self.cost(job.everything))
        # What would be left to pay if the comparison turned out as well as it can.
        left = plan.rest_candidates | (plan.import_floor or 0)
        fewer = [bit for bit in self.covmap.bits(left & plan.candidates) if self.bit_number[bit] >= 0]
        checking = self.machine * (
            self.overhead(job, fewer) + sum(job.estimates.get(bit, self.bit_cost[bit]) for bit in fewer)
        )
        then = min(checking, self.cost(fewer)) if fewer else 0.0
        return now - then > 2.0 * PROBE_COST * self.machine

    def spawn_probe(self, job: Job) -> None:
        if state.PINNED is None:
            state.load_pinned(ROOT)
            gc.freeze()
        self.sequence += 1
        spec = {
            "overlay": os.path.join(TMP, f"{job.plan.commit}-{self.sequence}"),
            "changes": state.plan_changes(job.plan),
        }
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                for other in self.running:
                    os.close(other)
            except OSError:
                pass
            state.probe_main(spec, write_fd, ROOT)
            os._exit(1)
        os.close(write_fd)
        try:
            os.setpgid(pid, pid)
        except OSError:
            pass
        run = Run(pid, job, [], time.monotonic())
        run.probe = True
        run.overlay = spec["overlay"]
        self.running[read_fd] = run

    def finish_probe(self, run: Run, used: float) -> None:
        """Replace the candidates of the import-time changes by the tests that can see what they left."""
        job = run.job
        plan = job.plan
        job.probed = True
        result = run.state
        narrowed = None
        if result is not None and run.fatal is None:
            own = {(path, first) for path, entries in plan.units.items() for first, _ in entries}
            try:
                narrowed = self.covmap.state_candidates(result, own)
            except Exception as error:
                plan.note(f"import state: {error!r}")
        if VERBOSE:
            found = "nothing" if result is None else (
                result.get("inconclusive") or f"{len(result['differences'])} differences"
            )
            log(f"{plan.commit}: import state compared using {used:.2f} CPU s: {found}")
        if narrowed is None:
            why = run.fatal or (result or {}).get("inconclusive") or "no narrower set"
            plan.note(f"import state: {why}")
            broken = (result or {}).get("failed") or []
            if result is None or run.fatal is not None or broken:
                # The patched package does not import as before (or that could
                # not be found out): every test that imports a changed or a
                # broken module is a candidate again.
                wide = plan.candidates | plan.import_wide
                for path in broken:
                    wide |= self.covmap.file_any(path)
                if wide != plan.candidates:
                    plan.candidates = wide
                    self.rebuild(job)
                    return
            self.queue.insert(0, job)
            return
        for path, first, lines in plan.warm_units:
            narrowed |= self.covmap.test_time(path, first, lines)
        before = plan.candidates.bit_count()
        plan.candidates = plan.rest_candidates | narrowed
        plan.import_check = 0
        plan.note(
            f"import state: {len(result['differences'])} differences, "
            f"{plan.candidates.bit_count()} of {before} tests can see them"
        )
        job.import_files = []
        self.rebuild(job)

    def rebuild(self, job: Job) -> None:
        """Queue the job again for the candidates its plan has now."""
        plan = job.plan
        bits = [
            bit
            for bit in self.covmap.bits(plan.candidates & ~plan.unverified)
            if self.bit_number[bit] >= 0
        ]
        if not bits and not job.import_files:
            self.finish(plan, set())
            return
        job.everything = set(bits)
        job.followers = {}
        job.estimates = {}
        job.slices = {}
        job.candidates = self.followers(plan, bits, job)
        self.estimate(plan, job.candidates, job)
        job.estimate = sum(job.estimates.values())
        self.queue.insert(0, job)

    def spawn(self, job: Job) -> None:
        tests = job.pending
        job.pending = []
        job.spawned += 1
        self.sequence += 1
        plan = job.plan
        changes: dict = {}
        for patch in plan.files:
            if patch.unapplied or patch.old_text == patch.new_text and patch.old_path in (None, patch.path):
                continue
            if patch.old_path and patch.old_path != patch.path:
                changes[patch.old_path] = None
            if patch.new_text is None:
                changes[patch.old_path or patch.path] = None
            else:
                changes[patch.path] = patch.new_text
        spec = {
            "mode": job.mode,
            "overlay": os.path.join(TMP, f"{plan.commit}-{self.sequence}"),
            "changes": changes,
            "swaps": plan.swaps if job.mode == "hot" else [],
            "redirects": plan.redirects if job.mode == "hot" else {},
            "tests": [(bit, self.covmap.ids[bit]) for bit in tests],
            "cases": self.case_filter(job, tests),
            "imports": job.import_files if job.mode == "clean" else [],
            "executor_tables": self.executor_tables(job, tests),
            "executor_memo": self.executor_memo_ok(job),
        }
        # The child keeps DuckDB closed until a test really asks it something (runner._ProbingConnection).
        spec["duckdb_probe"] = self.duckdb_record()
        spec["lazy"] = plan.lazy if job.mode == "hot" else []
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                for other in self.running:
                    os.close(other)
            except OSError:
                pass
            runner.child_main(spec, write_fd, ROOT, self.function_index)
            os._exit(1)
        os.close(write_fd)
        try:
            os.setpgid(pid, pid)
        except OSError:
            pass
        self.running[read_fd] = Run(pid, job, tests, time.monotonic())
        try:
            with open("/proc/self/statm", "rb") as handle:
                self.resident = int(handle.read().split()[1]) * _PAGE
        except (OSError, ValueError, IndexError):
            pass
        self.running[read_fd].overlay = spec["overlay"]

    def kill(self, run: Run) -> None:
        for target in (-run.pid, run.pid):
            try:
                os.kill(target, signal.SIGKILL)
            except OSError:
                pass

    def allowance(self, bit: int) -> float:
        """Wall-clock seconds a test may take before it counts as hung."""
        return max(4.0, 8.0 * self.bit_seconds[bit] + 3.0)

    def cpu_allowance(self, job: Job, bit: int) -> float:
        """CPU seconds a test may burn before it counts as failing.

        Well above what the whole test normally costs (with no recorded result
        reused): a patch that makes a test crawl would otherwise be paid for in
        CPU time until the wall-clock limit.
        """
        return max(3.0, 1.6 * self.worst_cost(job, bit) * max(self.machine, 1.0) + 1.0)

    def feed(self, run: Run, data: bytes, now: float) -> None:
        run.buffer += data
        *lines, run.buffer = run.buffer.split(b"\n")
        for line in lines:
            kind = line[:1]
            if kind == b"S":
                run.current = int(line[2:])
                run.current_started = now
                run.current_cpu = tree_cpu(run.pid)
                run.cpu_checked = now
                run.job.import_files = []
            elif kind == b"B":
                # A test file that can no longer be imported: all its tests fail.
                prefix = line[2:].decode("utf-8", "replace") + "::"
                broken = [bit for bit, test_id in enumerate(self.covmap.ids) if test_id.startswith(prefix)]
                run.job.failed.update(bit for bit in broken if self.bit_number[bit] >= 0)
                log(f"{run.job.plan.commit}: {prefix[:-2]} cannot be imported ({len(broken)} tests)")
            elif kind == b"R":
                _, bit, status = line.split()
                bit = int(bit)
                if bit not in run.done:
                    run.done.add(bit)
                    self.ran_seconds += self.bit_seconds[bit]
                if TRACE and now - run.current_started > 0.25:
                    log(
                        f"    {run.job.plan.commit}: {self.covmap.ids[bit].split('::')[-1]} took "
                        f"{now - run.current_started:.2f}s (recorded {self.bit_seconds[bit]:.2f}s)"
                        + (" FAILED" if status == b"1" else "")
                    )
                run.current = None
                run.current_cpu = tree_cpu(run.pid)
                run.cpu_checked = now
                if status == b"1":
                    run.job.failed.add(bit)
                    if not run.job.replanned and self.replan(run):
                        return
            elif kind == b"E":
                try:
                    run.state = json.loads(line[2:])
                except ValueError:
                    run.state = None
            elif kind == b"X":
                run.fatal = line[2:].decode("utf-8", "replace")
            elif kind == b"D":
                run.finished = True
                run.job.import_files = []
            run.last_event = now

    def close(self, fd: int) -> None:
        run = self.running.pop(fd)
        try:
            os.close(fd)
        except OSError:
            pass
        self.kill(run)
        used = 0.0
        try:
            _, _, usage = os.wait4(run.pid, 0)
            used = (usage.ru_utime + usage.ru_stime) * SLOWDOWN
        except OSError:
            pass
        if run.overlay:
            # A child that was killed leaves its patched tree behind; /tmp is small.
            shutil.rmtree(run.overlay, ignore_errors=True)
        job = run.job
        if run.probe:
            self.finish_probe(run, used)
            return
        left = [bit for bit in run.tests if bit not in run.done]
        if run.finished and not left and not job.failed and run.fatal is None:
            # A clean, complete run: compare what it cost with what was expected.
            expected = self.overhead(job, run.tests) + sum(job.estimates[bit] for bit in run.tests)
            if expected > 0.2:
                self.observed_cpu += used
                self.observed_base += expected
                prior = 3.0  # seconds of evidence the configured guess is worth
                self.machine = min(
                    4.0, max(0.3, (prior * MACHINE + self.observed_cpu) / (prior + self.observed_base))
                )
        if VERBOSE:
            log(
                f"{job.plan.commit}: {job.mode} child ran {len(run.done)}/{len(run.tests)} tests "
                f"({sum(self.bit_seconds[bit] for bit in run.done):.2f} recorded s) in "
                f"{time.monotonic() - run.started:.2f}s using {used:.2f} CPU s, {len(job.failed)} failed"
                + (", stopped early" if run.stopped else "")
                + f" [machine {self.machine:.2f}]"
            )
        if run.stopped:
            # replan() already split what was left into job.blind and job.pending.
            if job.pending:
                self.queue.insert(0, job)
            else:
                self.finish_job(job)
            return
        if run.fatal is not None:
            log(f"{job.plan.commit}: child failed in {job.mode} mode: {run.fatal}")
            if job.mode == "hot":
                job.mode = "clean"
                job.pending = left
                self.queue.insert(0, job)
            else:
                self.finish_job(job, left)
            return
        if run.finished and not left:
            self.finish_job(job)
            return
        # The child died or was killed part way.
        if run.current is not None and run.current in left:
            job.failed.add(run.current)
            left.remove(run.current)
        elif not run.done:
            job.strikes += 1
        if not left:
            self.finish_job(job)
        elif job.strikes >= 2 or job.timeouts >= 2 or job.spawned >= 8:
            log(f"{job.plan.commit}: giving up checking, {len(left)} tests selected unchecked")
            self.finish_job(job, left)
        else:
            job.pending = left
            self.queue.insert(0, job)

    def loop(self) -> None:
        while self.queue or self.running:
            now = time.monotonic()
            if now - self.start > HARD_DEADLINE:
                log("deadline reached: selecting everything still unchecked")
                for fd in list(self.running):
                    run = self.running.pop(fd)
                    self.kill(run)
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    left = [bit for bit in run.tests if bit not in run.done]
                    self.finish_job(run.job, left + run.job.pending)
                for job in self.queue:
                    self.finish_job(job, job.pending)
                self.queue = []
                return
            limit = 1 if now - self.start < SEQUENTIAL_UNTIL else MAX_PARALLEL
            while self.queue and len(self.running) < limit:
                job = self.queue.pop(0)
                if not job.decided and self.wants_probe(job):
                    self.spawn_probe(job)
                elif job.decided or self.decide(job):
                    self.spawn(job)
            ready, _, _ = select.select(list(self.running), [], [], 0.05)
            now = time.monotonic()
            for fd in ready:
                run = self.running[fd]
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if data:
                    self.feed(run, data, now)
                if not data or run.finished or run.fatal is not None or run.stopped:
                    self.close(fd)
            for fd, run in list(self.running.items()):
                if now > run.job.deadline:
                    log(f"{run.job.plan.commit}: over its time budget, the rest is selected unchecked")
                    run.job.timeouts = 99
                    self.close(fd)
                    continue
                used = None
                if now - run.cpu_checked > 0.25:
                    run.cpu_checked = now
                    cpu, resident = tree_usage(run.pid)
                    used = cpu - run.current_cpu
                    if resident > self.resident + MEMORY_ALLOWANCE:
                        # The run has 4 GB in all; a test that keeps allocating is stopped.
                        log(f"{run.job.plan.commit}: child over its memory allowance")
                        if run.current is None:
                            run.job.strikes += 1
                        else:
                            run.job.timeouts += 1
                        self.close(fd)
                        continue
                if run.current is not None:
                    over = now - run.current_started > self.allowance(run.current)
                    if used is not None and used > self.cpu_allowance(run.job, run.current):
                        over = True
                    if over:
                        log(f"{run.job.plan.commit}: {self.covmap.ids[run.current]} timed out")
                        run.job.timeouts += 1
                        self.close(fd)
                elif now - run.last_event > SETUP_ALLOWANCE or (
                    used is not None and used > SETUP_CPU * max(self.machine, 1.0)
                ):
                    log(f"{run.job.plan.commit}: child stalled outside a test")
                    run.job.strikes += 1
                    self.close(fd)

    # ---- driver ---------------------------------------------------------

    def run(self) -> dict[str, list[int]]:
        patches = sorted(glob.glob(os.path.join(DATA, "commits", "*.patch")))
        commits = [os.path.basename(path)[: -len(".patch")] for path in patches]
        loaded = {os.path.join(ROOT, path) for path in self.covmap.warm_files()}
        plans = []
        for commit, path in zip(commits, patches):
            try:
                with open(path, encoding="utf-8", errors="surrogateescape", newline="") as handle:
                    text = handle.read()
                plan = analyze(commit, text, self.covmap, ROOT, loaded)
            except Exception as error:
                plan = Plan(commit)
                plan.select_all = True
                plan.note(f"analysis raised {error!r}")
            plans.append(plan)
            try:
                self.plan_commit(plan)
            except Exception as error:
                plan.note(f"planning raised {error!r}")
                self.queue = [job for job in self.queue if job.plan is not plan]
                if plan.select_all:
                    self.decisions[commit] = list(self.numbers)
                else:
                    self.finish(plan, set(self.covmap.bits(plan.candidates)))
        analysed = time.monotonic()
        if self.queue:
            self.queue.sort(key=lambda job: job.estimate)
            try:
                self.warm_up()
                warmed = time.monotonic()
                self.loop()
            except Exception as error:
                log(f"running failed: {error!r}")
                warmed = time.monotonic()
                for run in self.running.values():
                    self.kill(run)
                    if run.job.plan.commit not in self.decisions:
                        self.finish_job(run.job, run.tests + run.job.pending)
                for job in self.queue:
                    if job.plan.commit not in self.decisions:
                        self.finish_job(job, job.pending)
        else:
            warmed = analysed
        for plan in plans:
            if plan.commit not in self.decisions:
                # Nothing above reached a verdict: every candidate is selected.
                self.finish(plan, set(self.covmap.bits(plan.candidates)))
            if VERBOSE:
                log(f"{plan.commit}: {len(self.decisions[plan.commit])} selected; " + "; ".join(plan.notes))
        done = time.monotonic()
        log(
            f"{len(commits)} commits: analysis {analysed - self.start:.2f}s, warm-up "
            f"{warmed - analysed:.2f}s, checking {done - warmed:.2f}s, ran {self.ran_seconds:.1f} "
            f"recorded test seconds"
        )
        return {commit: self.decisions[commit] for commit in commits}


def main() -> None:
    out_fd = os.dup(1)
    os.dup2(2, 1)  # nothing but the answers may reach stdout
    lines = []
    try:
        coordinator = Coordinator()
        decisions = coordinator.run()
        for commit, numbers in decisions.items():
            workers = schedule.split([(coordinator.ms[number], number) for number in numbers])
            lines.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
    except BaseException as error:  # noqa: BLE001 - last resort: run everything
        log(f"selector failed, falling back to every test: {error!r}")
        tests = json.load(open(os.path.join(DATA, "tests.json"), encoding="utf-8"))["tests"]
        workers = schedule.split([(int(t["seconds"] * 1000 + 0.5), t["number"]) for t in tests])
        lines = [
            json.dumps({"commit": os.path.basename(path)[: -len(".patch")], "workers": workers})
            for path in sorted(glob.glob(os.path.join(DATA, "commits", "*.patch")))
        ]
    payload = ("\n".join(lines) + "\n").encode() if lines else b""
    view = memoryview(payload)
    while view:
        view = view[os.write(out_fd, view):]
    os._exit(0)


if __name__ == "__main__":
    main()
