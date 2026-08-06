# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Runner protocol and shared helpers for benchmark runners."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from tqdm.asyncio import tqdm as tqdm_asyncio

from benchmarks.client.openai_client import (
    OpenAICompatClient,
    derive_max_connections,
)
from benchmarks.config import BenchConfig
from benchmarks.metrics.aggregator import (
    MetricsAggregator,
    attach_user_throughput,
)
from benchmarks.report.summary import log_summary
from benchmarks.storage.result_writer import ResultWriter

logger = logging.getLogger(__name__)


class Runner(ABC):
    """Benchmark runner: one ``async run()`` entry plus shared load helpers."""

    def __init__(self, config: BenchConfig):
        self.config = config

    @abstractmethod
    async def run(self) -> dict[str, Any]:
        """Execute the benchmark and return a result dict."""

    def make_client(self) -> OpenAICompatClient:
        """Build an OpenAI-compatible client from ``config.target`` / load."""
        t = self.config.target
        load = self.config.load
        return OpenAICompatClient(
            t.url,
            t.model,
            timeout=t.timeout,
            api_key=t.api_key,
            max_connections=derive_max_connections(
                parallel=load.primary_parallel,
                number=load.primary_number,
                open_loop=load.open_loop,
            ),
        )

    def make_writer(self) -> ResultWriter:
        """Create a timestamped result writer under ``config.output``."""
        return ResultWriter(root_dir=self.config.output.outputs_dir)

    def primary_load(self) -> dict[str, Any]:
        """Return primary load-point fields from ``config.load``."""
        load = self.config.load
        parallel = load.primary_parallel
        open_loop = load.open_loop
        return {
            "parallel": parallel,
            "number": load.primary_number,
            "rate": load.primary_rate,
            "open_loop": open_loop,
            "resolved_parallel": -1 if open_loop else parallel,
        }

    def build_run_config(self, mode: str, load: dict[str, Any]) -> dict[str, Any]:
        """Build the per-run config dict used by summary and persistence."""
        cfg = self.config
        return {
            "mode": mode,
            "model": cfg.target.model,
            "url": cfg.target.url,
            "parallel": load["parallel"],
            "number": load["number"],
            "rate": load["rate"],
            "open_loop": load["open_loop"],
            "stream": cfg.generation.stream,
            "resolved": {
                "parallel": load["resolved_parallel"],
                "number": load["number"],
                "rate": load["rate"],
            },
        }

    async def dispatch(
        self,
        client: OpenAICompatClient,
        requests: list[dict[str, Any]],
        *,
        parallel: int,
        rate: float,
        open_loop: bool,
    ) -> dict[str, Any]:
        """Dispatch requests with closed/open-loop concurrency and optional rate pacing."""
        gen = self.config.generation
        max_tokens = gen.max_tokens
        temperature = gen.temperature

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
                result = await client.generate_stream(
                    prompt=req.get("prompt"),
                    messages=req.get("messages"),
                    tools=req.get("tools"),
                    max_tokens=max_tokens,
                    temperature=temperature,
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
        logger.info("Benchmark finished!")
        return {
            "results": [r for r in results if r is not None],
            "total_time": end - start,
        }

    def aggregate_metrics(
        self,
        raw: dict[str, Any],
        *,
        rate: float,
        number: int,
        resolved_parallel: int,
    ) -> dict[str, Any]:
        """Aggregate raw dispatch output and attach per-user throughput."""
        metrics = MetricsAggregator().aggregate(raw)
        metrics["rate"] = rate
        metrics["number"] = number
        attach_user_throughput(metrics, parallel=resolved_parallel)
        return metrics

    def save_results(
        self,
        writer: ResultWriter,
        run_config: dict[str, Any],
        raw: dict[str, Any],
        metrics: dict[str, Any],
    ) -> None:
        """Log summary and persist config / raw / metrics JSON artifacts."""
        log_summary(run_config, metrics)
        writer.save_json(
            "config.json", {**self.config.to_dict(), **run_config}
        )
        writer.save_json("raw_output.json", raw["results"])
        writer.save_json("metrics.json", metrics)
        logger.info("Results saved: %s", writer.output_dir)
