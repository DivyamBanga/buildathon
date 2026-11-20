"""Blast Radius selector: one JSON line per commit on stdout, logs on stderr.

For every patch:
  1. analyze.py names the tests that could notice the change (dependency map).
  2. Those tests are run against the patched code in forked children
     (runner.py) and the ones that fail are selected. Tests that cost more to
     check than to hand to a worker are selected without being run.
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
import signal
import sys
import time
import types

from . import runner, schedule
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
MAX_PARALLEL = 2
# CPU seconds this machine needs per recorded test second, to weigh checking
# a test against simply selecting it.
RATIO = float(os.environ.get("BR_RATIO", "1.0"))
# Tests longer than this are selected rather than run when they are candidates.
BLIND_SECONDS = float(os.environ.get("BR_BLIND", "2.5"))
SETUP_ALLOWANCE = 40.0
VERBOSE = os.environ.get("BR_VERBOSE", "") != ""


def log(*parts) -> None:
    print(*parts, file=sys.stderr, flush=True)


class Job:
    def __init__(self, plan: Plan, mode: str, pending: list[int], blind: set[int]) -> None:
        self.plan = plan
        self.mode = mode
        self.pending = pending  # map bits still to run, in test order
        self.blind = blind  # selected without running
        self.failed: set[int] = set()
        self.strikes = 0
        self.timeouts = 0
        self.spawned = 0
        self.estimate = 0.0


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

    # ---- deciding -------------------------------------------------------

    def cost(self, bits) -> float:
        return schedule.cost_ms([self.ms[self.bit_number[bit]] for bit in bits]) / 1000.0

    def plan_commit(self, plan: Plan) -> None:
        if plan.select_all:
            self.decisions[plan.commit] = list(self.numbers)
            return
        bits = [bit for bit in self.covmap.bits(plan.candidates) if self.bit_number[bit] >= 0]
        if not bits:
            self.decisions[plan.commit] = []
            return
        blind = [bit for bit in bits if self.bit_seconds[bit] > BLIND_SECONDS]
        rest = [bit for bit in bits if self.bit_seconds[bit] <= BLIND_SECONDS]
        mode = "clean" if plan.needs_clean else "hot"
        overhead = 0.5 if mode == "clean" else 0.05
        check = overhead + RATIO * sum(self.bit_seconds[bit] for bit in rest)
        saving = self.cost(bits) - self.cost(blind)
        if not rest or check >= saving:
            plan.note(f"selected unchecked: checking {check:.1f}s >= saving {saving:.1f}s")
            self.finish(plan, set(bits))
            return
        job = Job(plan, mode, rest, set(blind))
        job.estimate = check
        self.queue.append(job)

    def finish(self, plan: Plan, bits: set[int]) -> None:
        numbers = sorted(self.bit_number[bit] for bit in bits if self.bit_number[bit] >= 0)
        if plan.candidates:
            numbers = sorted(set(numbers).union(self.unmapped))
        self.decisions[plan.commit] = numbers

    def finish_job(self, job: Job, unchecked=()) -> None:
        self.finish(job.plan, job.blind | job.failed | set(unchecked))

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
            runner.child_main(spec, write_fd, ROOT, self.function_index)
            os._exit(1)
        os.close(write_fd)
        try:
            os.setpgid(pid, pid)
        except OSError:
            pass
        self.running[read_fd] = Run(pid, job, tests, time.monotonic())

    def kill(self, run: Run) -> None:
        for target in (-run.pid, run.pid):
            try:
                os.kill(target, signal.SIGKILL)
            except OSError:
                pass

    def allowance(self, bit: int) -> float:
        return max(4.0, 8.0 * self.bit_seconds[bit] + 3.0)

    def feed(self, run: Run, data: bytes, now: float) -> None:
        run.buffer += data
        *lines, run.buffer = run.buffer.split(b"\n")
        for line in lines:
            kind = line[:1]
            if kind == b"S":
                run.current = int(line[2:])
                run.current_started = now
            elif kind == b"R":
                _, bit, status = line.split()
                bit = int(bit)
                if status == b"1":
                    run.job.failed.add(bit)
                if bit not in run.done:
                    run.done.add(bit)
                    self.ran_seconds += self.bit_seconds[bit]
                run.current = None
            elif kind == b"X":
                run.fatal = line[2:].decode("utf-8", "replace")
            elif kind == b"D":
                run.finished = True
            run.last_event = now

    def close(self, fd: int) -> None:
        run = self.running.pop(fd)
        try:
            os.close(fd)
        except OSError:
            pass
        self.kill(run)
        try:
            os.waitpid(run.pid, 0)
        except OSError:
            pass
        job = run.job
        left = [bit for bit in run.tests if bit not in run.done]
        if VERBOSE:
            log(
                f"{job.plan.commit}: {job.mode} child ran {len(run.done)}/{len(run.tests)} tests "
                f"({sum(self.bit_seconds[bit] for bit in run.done):.2f} recorded s) in "
                f"{time.monotonic() - run.started:.2f}s, {len(job.failed)} failed"
            )
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
                    self.finish_job(run.job, [bit for bit in run.tests if bit not in run.done])
                for job in self.queue:
                    self.finish_job(job, job.pending)
                self.queue = []
                return
            while self.queue and len(self.running) < MAX_PARALLEL:
                self.spawn(self.queue.pop(0))
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
                if not data or run.finished or run.fatal is not None:
                    self.close(fd)
            for fd, run in list(self.running.items()):
                if run.current is not None:
                    if now - run.current_started > self.allowance(run.current):
                        log(f"{run.job.plan.commit}: {self.covmap.ids[run.current]} timed out")
                        run.job.timeouts += 1
                        self.close(fd)
                elif now - run.last_event > SETUP_ALLOWANCE:
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
                self.decisions[commit] = list(self.numbers)
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
                        self.finish_job(run.job, run.tests)
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
