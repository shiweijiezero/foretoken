# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Single-point benchmark: load → dispatch → aggregate → persist → print."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Optional

from tqdm.asyncio import tqdm as tqdm_asyncio

from benchmarks.client.openai_client import OpenAICompatClient
from benchmarks.config import BenchConfig
from benchmarks.metrics.aggregator import (
    MetricsAggregator,
    attach_user_throughput,
)
from benchmarks.report.summary import print_summary
from benchmarks.runner.base import Runner
from benchmarks.storage.result_writer import ResultWriter
from benchmarks.workload.loader import load_requests


class SingleRunner(Runner):
    """Run one closed-loop or open-loop load point.

    Semantics (EvalScope-aligned):
    - Default closed-loop: semaphore = ``parallel``.
    - ``rate > 0``: Poisson absolute-time pacing.
    - ``open_loop``: fire on schedule without semaphore backpressure.
    """

    async def run(self) -> dict[str, Any]:
        cfg = self.config
        parallel = cfg.load.primary_parallel
        number = cfg.load.primary_number
        rate = cfg.load.primary_rate
        open_loop = cfg.load.open_loop
        resolved_parallel = -1 if open_loop else parallel

        requests = load_requests(cfg)
        writer = ResultWriter(root_dir=cfg.output.outputs_dir)
        aggregator = MetricsAggregator()

        run_config = {
            "mode": "single",
            "model": cfg.target.model,
            "url": cfg.target.url,
            "parallel": parallel,
            "number": number,
            "rate": rate,
            "open_loop": open_loop,
            "stream": cfg.generation.stream,
            "resolved": {
                "parallel": resolved_parallel,
                "number": number,
                "rate": rate,
            },
        }

        client = OpenAICompatClient(
            cfg.target.url,
            cfg.target.model,
            timeout=cfg.target.timeout,
            api_key=cfg.target.api_key,
        )

        raw = await self._dispatch(
            client,
            requests,
            parallel=parallel,
            rate=rate,
            open_loop=open_loop,
            max_tokens=cfg.generation.max_tokens,
            temperature=cfg.generation.temperature,
            stream=cfg.generation.stream,
        )

        metrics = aggregator.aggregate(raw)
        metrics["rate"] = rate
        metrics["number"] = number
        attach_user_throughput(metrics, parallel=resolved_parallel)

        print_summary(run_config, metrics)
        writer.save_json("config.json", {**cfg.to_dict(), **run_config})
        writer.save_json("raw_output.json", raw["results"])
        writer.save_json("metrics.json", metrics)
        print(f"\nResults saved: {writer.output_dir}")

        return {
            "mode": "single",
            "metrics": metrics,
            "output_dir": writer.output_dir,
        }

    async def _dispatch(
        self,
        client: OpenAICompatClient,
        requests: list[dict[str, Any]],
        *,
        parallel: int,
        rate: float,
        open_loop: bool,
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict[str, Any]:
        await client.start()
        n = len(requests)
        has_pacing = rate != -1 and rate > 0
        sem: Optional[asyncio.Semaphore] = (
            None if open_loop else asyncio.Semaphore(max(parallel, 1))
        )
        results: list[Optional[dict[str, Any]]] = [None] * n
        start = time.perf_counter()
        pbar = tqdm_asyncio(total=n, desc="Benchmarking")

        async def one(i: int, req: dict[str, Any]) -> None:
            if sem is not None:
                await sem.acquire()
            try:
                result = await client.generate(
                    prompt=req.get("prompt"),
                    messages=req.get("messages"),
                    tools=req.get("tools"),
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=stream,
                )
                results[i] = result
            finally:
                if sem is not None:
                    sem.release()
                pbar.update(1)

        try:
            if has_pacing:
                tasks: list[asyncio.Task[None]] = []
                t0 = time.perf_counter()
                next_at = 0.0
                for i, req in enumerate(requests):
                    delay = next_at - (time.perf_counter() - t0)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    tasks.append(asyncio.create_task(one(i, req)))
                    # Poisson inter-arrival ~ Exp(rate)
                    next_at += random.expovariate(rate)
                await asyncio.gather(*tasks)
            else:
                await asyncio.gather(
                    *[one(i, req) for i, req in enumerate(requests)]
                )
        finally:
            pbar.close()
            await client.close()

        end = time.perf_counter()
        print("\nBenchmark finished!")
        return {
            "results": [r for r in results if r is not None],
            "total_time": end - start,
        }
