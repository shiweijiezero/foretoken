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
from benchmarks.logger.wandb import WandbLogger
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
        """Build an OpenAI-compatible client from ``config.endpoint`` / load."""
        endpoint = self.config.endpoint
        load = self.config.load
        return OpenAICompatClient(
            endpoint.url,
            endpoint.model,
            timeout=endpoint.timeout,
            api_key=endpoint.api_key,
            max_connections=derive_max_connections(
                parallel=load.parallel[0],
                number=load.number[0],
                open_loop=load.open_loop,
            ),
            max_retries=endpoint.max_retries,
            headers=endpoint.headers,
        )

    def make_writer(self) -> ResultWriter:
        """Create the local JSON writer selected for this benchmark."""
        return ResultWriter(
            root_dir=self.config.output.output_dir,
            enabled=self.config.output.includes("local"),
        )

    def make_wandb_logger(
        self,
        writer: ResultWriter,
        load: dict[str, Any],
        *,
        name_suffix: Optional[str] = None,
        group: Optional[str] = None,
        config: Optional[BenchConfig] = None,
    ) -> WandbLogger:
        """Start W&B when selected as a result destination."""
        wandb_logger = WandbLogger()
        wandb_logger.start(
            config if config is not None else self.config,
            output_dir=writer.output_dir,
            parallel=int(load["resolved_parallel"]),
            rate=float(load["rate"]),
            name_suffix=name_suffix,
            group=group,
        )
        return wandb_logger

    def default_load(self) -> dict[str, Any]:
        """Return the first load-point fields from ``config.load``."""
        load = self.config.load
        parallel = load.parallel[0]
        open_loop = load.open_loop
        return {
            "parallel": parallel,
            "number": load.number[0],
            "rate": float(load.rate[0]),
            "open_loop": open_loop,
            "resolved_parallel": -1 if open_loop else parallel,
        }

    def build_run_config(self, mode: str, load: dict[str, Any]) -> dict[str, Any]:
        """Build the per-run config dict used by summary and persistence."""
        config = self.config
        return {
            "mode": mode,
            "model": config.endpoint.model,
            "url": config.endpoint.url,
            "parallel": load["parallel"],
            "number": load["number"],
            "rate": load["rate"],
            "open_loop": load["open_loop"],
            "stream": config.generation.stream,
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
        generation = self.config.generation
        max_tokens = generation.max_tokens
        extra_body = generation.request_overrides()

        request_count = len(requests)
        has_pacing = rate != -1 and rate > 0
        semaphore: Optional[asyncio.Semaphore] = (
            None if open_loop else asyncio.Semaphore(parallel)
        )
        results: list[Optional[dict[str, Any]]] = [None] * request_count
        start_time = time.perf_counter()
        progress_bar = tqdm_asyncio(
            total=request_count,
            desc="Benchmarking",
            disable=self.config.output.includes("quiet"),
        )

        async def dispatch_one(index: int, request: dict[str, Any]) -> None:
            if semaphore is not None:
                await semaphore.acquire()
            try:
                result = await client.generate(
                    prompt=request.get("prompt"),
                    messages=request.get("messages"),
                    tools=request.get("tools"),
                    max_tokens=max_tokens,
                    stream=generation.stream,
                    extra_body=extra_body,
                )
                results[index] = result
            finally:
                if semaphore is not None:
                    semaphore.release()
                progress_bar.update(1)

        try:
            if has_pacing:
                tasks: list[asyncio.Task[None]] = []
                pacing_start = time.perf_counter()
                next_at = 0.0
                for index, request in enumerate(requests):
                    delay = next_at - (time.perf_counter() - pacing_start)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    tasks.append(asyncio.create_task(dispatch_one(index, request)))
                    # Poisson inter-arrival ~ Exp(rate)
                    next_at += random.expovariate(rate)
                await asyncio.gather(*tasks)
            else:
                await asyncio.gather(
                    *[
                        dispatch_one(index, request)
                        for index, request in enumerate(requests)
                    ]
                )
        finally:
            progress_bar.close()
            await client.close()

        end_time = time.perf_counter()
        logger.info("Benchmark finished!")
        if any(result is None for result in results):
            raise RuntimeError("dispatch finished with missing request results")
        return {
            "results": results,
            "total_time": end_time - start_time,
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
        *,
        wandb_logger: Optional[WandbLogger] = None,
        config_snapshot: Optional[dict[str, Any]] = None,
    ) -> None:
        """Publish the selected console, JSON, and W&B results."""
        if not self.config.output.includes("quiet"):
            log_summary(run_config, metrics)
        base = config_snapshot if config_snapshot is not None else self.config.to_dict()
        writer.save_json("config.json", {**base, **run_config})
        writer.save_json("raw_output.json", raw["results"])
        writer.save_json("metrics.json", metrics)
        if wandb_logger is not None:
            try:
                wandb_logger.log_metrics(metrics)
            finally:
                wandb_logger.finish()
        if writer.enabled:
            logger.info("Results saved: %s", writer.output_dir)
