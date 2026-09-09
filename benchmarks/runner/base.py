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

from evalscope.perf.multi_turn_args import _sample_int_or_range as sample_max_tokens
from tqdm.asyncio import tqdm as tqdm_asyncio

from benchmarks.client.openai_client import (
    OpenAICompatClient,
    derive_max_connections,
)
from benchmarks.config import BenchConfig, LoadConfig
from benchmarks.logger.wandb import WandbLogger
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
        self._generation_overrides = config.generation.request_overrides()

    @abstractmethod
    async def run(self) -> dict[str, Any]:
        """Execute the benchmark and return a result dict."""

    def create_client(self, parallel: int, number: int) -> OpenAICompatClient:
        """Create an OpenAI-compatible client sized for one load point.

        Caller owns ``close()``. Pool capacity follows ``parallel`` /
        ``number`` / ``open_loop`` for that point only.
        """
        endpoint = self.config.endpoint
        load = self.config.load
        return OpenAICompatClient(
            endpoint.url,
            endpoint.model,
            timeout=endpoint.timeout,
            api_key=endpoint.api_key,
            max_connections=derive_max_connections(
                parallel=parallel,
                number=number,
                open_loop=load.open_loop,
            ),
            max_retries=endpoint.max_retries,
            headers=endpoint.headers,
        )

    def create_writer(self, output_dir: Optional[str] = None) -> ResultWriter:
        """Create a local JSON writer for this run or experiment root.

        With ``output_dir``, use that exact artifact directory (child point /
        source). Otherwise create a timestamp under ``config.output.output_dir``.
        """
        enabled = self.config.output.includes("local")
        if output_dir is not None:
            return ResultWriter(output_dir=output_dir, enabled=enabled)
        return ResultWriter(
            root_dir=self.config.output.output_dir,
            enabled=enabled,
        )

    def create_wandb_logger(
        self,
        writer: ResultWriter,
        load: dict[str, Any],
        name_suffix: Optional[str] = None,
        group: Optional[str] = None,
    ) -> WandbLogger:
        """Start a W&B run for one load point when W&B is a result destination.

        Caller owns ``finish()``.
        """
        wandb_logger = WandbLogger()
        wandb_logger.start(
            self.config,
            output_dir=writer.output_dir,
            parallel=int(load["resolved_parallel"]),
            rate=float(load["rate"]),
            name_suffix=name_suffix,
            group=group,
        )
        return wandb_logger

    def default_load(self) -> dict[str, Any]:
        """Return the load-point fields from ``config.load``."""
        load = self.config.load
        parallel = int(load.parallel)
        open_loop = load.open_loop
        return {
            "parallel": parallel,
            "number": int(load.number),
            "rate": float(load.rate),
            "open_loop": open_loop,
            "resolved_parallel": -1 if open_loop else parallel,
        }

    def build_run_config(self, mode: str, load: dict[str, Any]) -> dict[str, Any]:
        """Build the per-run config dict used by summary and persistence."""
        config = self.config
        run_config = {
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
        if config.dataset.dataset == ["random"]:
            run_config["random_seed"] = config.dataset.random_seed
        return run_config

    async def generate_request(
        self,
        client: OpenAICompatClient,
        *,
        prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send one request using the runner's resolved generation config."""
        generation = self.config.generation
        return await client.generate(
            prompt=prompt,
            messages=messages,
            tools=tools,
            max_tokens=sample_max_tokens(generation.max_tokens),
            stream=generation.stream,
            extra_body=self._generation_overrides,
        )

    async def dispatch(
        self,
        client: OpenAICompatClient,
        requests: list[dict[str, Any]],
        *,
        parallel: int,
        rate: float,
        open_loop: bool,
    ) -> dict[str, Any]:
        """Dispatch requests with closed/open-loop concurrency and optional rate pacing.

        Does not close ``client``; the caller that created it owns cleanup.
        """
        request_count = len(requests)
        LoadConfig.validate_point(parallel=int(parallel), rate=float(rate))
        has_pacing = float(rate) > 0
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
                result = await self.generate_request(
                    client,
                    prompt=request.get("prompt"),
                    messages=request.get("messages"),
                    tools=request.get("tools"),
                )
                result["end_time"] = time.perf_counter() - start_time
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
        include_user_throughput: bool = True,
    ) -> dict[str, Any]:
        """Aggregate raw output and optionally attach per-user throughput."""
        metrics = MetricsAggregator().aggregate(raw)
        configured_stream = bool(self.config.generation.stream)
        if metrics["stream"] != configured_stream:
            raise RuntimeError(
                "recorded stream mode does not match the requests that ran: "
                f"config={configured_stream} results={metrics['stream']}"
            )
        metrics["rate"] = rate
        metrics["number"] = number
        metrics["parallel"] = resolved_parallel
        if include_user_throughput:
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
        wandb_trace_results: Optional[list[dict[str, Any]]] = None,
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
                if wandb_trace_results is not None:
                    wandb_logger.log_trace_results(wandb_trace_results)
                wandb_logger.log_metrics(metrics)
            finally:
                wandb_logger.finish()
        if writer.enabled:
            logger.info("Results saved: %s", writer.output_dir)
