# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Schedule request episodes on one clock with shared concurrency and duration admission."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

import numpy as np

from benchmarks.config.benchmark import HttpLoadSchedule
from benchmarks.datasets.workload import RequestEpisode

Result = TypeVar("Result")


async def run_schedule(
    episodes: Iterable[RequestEpisode],
    execute: Callable[[int, RequestEpisode, float, float | None], Awaitable[Result]],
    load: HttpLoadSchedule,
    *,
    seed: int,
    trace_start: float | None = None,
    before_first: Callable[[], Awaitable[None]] | None = None,
) -> tuple[list[Result], float, float]:
    """Admit episodes by arrival time, drain admitted work, and return the measured window.

    Recorded arrivals retain their relative spacing under concurrency pressure.
    Duration limits admission; requests already sent are allowed to finish.
    """
    iterator = iter(episodes)
    first = next(iterator, None)
    rng = np.random.RandomState(seed)
    results: dict[int, Result] = {}
    active: set[asyncio.Task[None]] = set()
    if first is not None and before_first is not None:
        await before_first()
    if first is not None and trace_start is not None:
        # Replay begins with the first selected event; original window offsets
        # remain in the trace records. Capture setup precedes the real clock.
        trace_start = first.trace.timestamp_seconds
    started = time.perf_counter()
    deadline = (
        started + load.duration_seconds if load.duration_seconds is not None else None
    )

    async def run_one(index: int, episode: RequestEpisode, scheduled_at: float) -> None:
        results[index] = await execute(index, episode, scheduled_at, deadline)

    offset = 0.0
    episode = first
    index = 0
    async with asyncio.TaskGroup() as group:
        while episode is not None:
            if trace_start is not None:
                offset = episode.trace.timestamp_seconds - trace_start
            elif index and load.arrival_rate > 0:
                if load.arrival_pattern == "constant":
                    offset += 1.0 / load.arrival_rate
                else:
                    shape = (
                        1.0 if load.arrival_pattern == "poisson" else load.burstiness
                    )
                    offset += float(rng.gamma(shape, 1.0 / (load.arrival_rate * shape)))
            scheduled_at = started + offset
            if deadline is not None and scheduled_at >= deadline:
                break
            delay = scheduled_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            if load.max_concurrency > 0 and len(active) >= load.max_concurrency:
                remaining = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.perf_counter())
                )
                await asyncio.wait(
                    active, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
            if deadline is not None and time.perf_counter() >= deadline:
                break
            task = group.create_task(run_one(index, episode, scheduled_at))
            active.add(task)
            task.add_done_callback(active.discard)
            index += 1
            episode = next(iterator, None)
        # TaskGroup drains successful work and cancels peers on an execution error.
    return (
        [results[index] for index in sorted(results)],
        time.perf_counter() - started,
        started,
    )
