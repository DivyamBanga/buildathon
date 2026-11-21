"""Split the chosen tests across workers.

A commit costs its busiest worker's time plus 2 s for every worker, so more
workers only pay off while they take more than 2 s off the busiest one.
"""

from __future__ import annotations

WORKER_COST_MS = 2_000
MAX_WORKERS = 8


def _lpt(items: list[tuple[int, int]], worker_count: int) -> tuple[int, list[list[int]]]:
    """Longest first, each onto the worker with the least so far."""
    loads = [0] * worker_count
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    for duration, number in items:
        lightest = min(range(worker_count), key=loads.__getitem__)
        loads[lightest] += duration
        workers[lightest].append(number)
    return max(loads), workers


def _first_fit(items: list[tuple[int, int]], worker_count: int, capacity: int):
    """First-fit decreasing into bins of `capacity`; None if it needs more bins."""
    loads = [0] * worker_count
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    for duration, number in items:
        for index in range(worker_count):
            if loads[index] + duration <= capacity:
                loads[index] += duration
                workers[index].append(number)
                break
        else:
            return None
    return max(loads), workers


def _best_for(items: list[tuple[int, int]], worker_count: int) -> tuple[int, list[list[int]]]:
    total = sum(duration for duration, _ in items)
    longest = items[0][0]
    lower = max(longest, -(-total // worker_count))
    best_load, best = _lpt(items, worker_count)
    if best_load == lower:
        return best_load, best
    # Multifit: binary search on the capacity that first-fit decreasing can meet.
    low, high = lower, best_load
    while low < high:
        middle = (low + high) // 2
        packed = _first_fit(items, worker_count, middle)
        if packed is None:
            low = middle + 1
        else:
            high = middle
            if packed[0] < best_load:
                best_load, best = packed
    return best_load, best


def _plan(items: list[tuple[int, int]]) -> tuple[int, list[list[int]]]:
    """items sorted longest first. Returns (cost in ms, workers)."""
    longest = items[0][0]
    best_cost = -1
    best_workers: list[list[int]] = []
    for worker_count in range(1, min(MAX_WORKERS, len(items)) + 1):
        load, workers = _best_for(items, worker_count)
        occupied = [worker for worker in workers if worker]
        cost = load + WORKER_COST_MS * len(occupied)
        if best_cost < 0 or cost < best_cost:
            best_cost = cost
            best_workers = occupied
        if load - longest <= WORKER_COST_MS:
            # Another worker costs 2 s and cannot take the busiest one below
            # the single longest test.
            break
    return best_cost, best_workers


def cost_ms(durations: list[int]) -> int:
    if not durations:
        return 0
    return _plan(sorted(((d, i) for i, d in enumerate(durations)), reverse=True))[0]


def split(tests: list[tuple[int, int]]) -> list[list[int]]:
    """tests: (duration in ms, test number). Returns the workers' test numbers."""
    if not tests:
        return []
    return _plan(sorted(tests, reverse=True))[1]
