# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Thin Weights & Biases logger for progressive and final benchmark metrics."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import wandb

from benchmarks.config import BenchConfig, WandbConfig

logger = logging.getLogger(__name__)

# Short benches (e.g. --dataset random) often finish under the W&B default
# ~15s system-stats interval and get no System section; sample every 1s.
_SYSTEM_STATS_INTERVAL_S = 1.0

# Stable W&B chart keys (LLM subset).
_TIME_TAKEN = "Test Duration (s)"
_CONCURRENCY = "Concurrency"
_REQUEST_RATE = "Request Rate (req/s)"
_TOTAL_REQUESTS = "Total Requests"
_SUCCEED_REQUESTS = "Success Requests"
_FAILED_REQUESTS = "Failed Requests"
_REQUEST_THROUGHPUT = "Req Throughput (req/s)"
_AVERAGE_LATENCY = "Avg Latency (s)"
_AVERAGE_INPUT_TOKENS = "Avg Input Tokens"
_OUTPUT_TOKEN_THROUGHPUT = "Output Throughput (tok/s)"
_TOTAL_TOKEN_THROUGHPUT = "Total Throughput (tok/s)"
_AVERAGE_TTFT = "Avg TTFT (ms)"
_AVERAGE_TPOT = "Avg TPOT (ms)"
_AVERAGE_ITL = "Avg ITL (ms)"
_AVERAGE_OUTPUT_TOKENS = "Avg Output Tokens"


@dataclass
class _RunningAverages:
    """O(1) running averages for progressive W&B steps."""

    concurrency: int = 0
    rate: float = -1.0
    start_time: float = field(default_factory=time.perf_counter)
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    total_latency: float = 0.0
    total_ttft: float = 0.0
    ttft_count: int = 0
    total_tpot: float = 0.0
    tpot_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def update(self, result: dict[str, Any]) -> dict[str, Any]:
        self.total_count += 1
        if not result["success"]:
            self.failed_count += 1
            return self.to_message()

        self.success_count += 1
        self.total_latency += float(result["latency"])
        self.total_input_tokens += int(result["input_tokens"])
        self.total_output_tokens += int(result["output_tokens"])
        ttft = result["ttft"]
        if ttft is not None:
            self.total_ttft += float(ttft)
            self.ttft_count += 1
        tpot = result["tpot"]
        if tpot is not None:
            self.total_tpot += float(tpot)
            self.tpot_count += 1
        return self.to_message()

    def to_message(self, ndigits: int = 4) -> dict[str, Any]:
        elapsed = time.perf_counter() - self.start_time
        message: dict[str, Any] = {
            _TIME_TAKEN: round(elapsed, ndigits),
            _CONCURRENCY: self.concurrency,
            _REQUEST_RATE: self.rate,
            _TOTAL_REQUESTS: self.total_count,
            _SUCCEED_REQUESTS: self.success_count,
            _FAILED_REQUESTS: self.failed_count,
            _REQUEST_THROUGHPUT: round(self.total_count / elapsed, ndigits),
            _OUTPUT_TOKEN_THROUGHPUT: round(
                self.total_output_tokens / elapsed, ndigits
            ),
            _TOTAL_TOKEN_THROUGHPUT: round(
                (self.total_input_tokens + self.total_output_tokens) / elapsed,
                ndigits,
            ),
        }
        if self.success_count:
            success_count = self.success_count
            message[_AVERAGE_LATENCY] = round(
                self.total_latency / success_count, ndigits
            )
            message[_AVERAGE_INPUT_TOKENS] = round(
                self.total_input_tokens / success_count, ndigits
            )
            message[_AVERAGE_OUTPUT_TOKENS] = round(
                self.total_output_tokens / success_count, ndigits
            )
        if self.ttft_count:
            message[_AVERAGE_TTFT] = round(
                self.total_ttft / self.ttft_count * 1000, 2
            )
        if self.tpot_count:
            avg_tpot_ms = round(self.total_tpot / self.tpot_count * 1000, 2)
            message[_AVERAGE_TPOT] = avg_tpot_ms
            message[_AVERAGE_ITL] = avg_tpot_ms
        return message


def metrics_to_wandb_message(metrics: dict[str, Any]) -> dict[str, Any]:
    """Map Foretoken ``metrics.json`` to W&B chart keys."""
    throughput = metrics["throughput"]
    latency = metrics["latency"]
    ttft = metrics["ttft"]
    tpot = metrics["tpot"]

    message = {
        _TIME_TAKEN: round(float(metrics["benchmark_time"]), 4),
        _CONCURRENCY: int(metrics["parallel"]),
        _REQUEST_RATE: float(metrics["rate"]),
        _TOTAL_REQUESTS: int(metrics["request_num"]),
        _SUCCEED_REQUESTS: int(metrics["success_num"]),
        _FAILED_REQUESTS: int(metrics["failed_num"]),
        _REQUEST_THROUGHPUT: round(float(throughput["request/s"]), 4),
        _OUTPUT_TOKEN_THROUGHPUT: round(float(throughput["token/s"]), 4),
        _TOTAL_TOKEN_THROUGHPUT: round(float(throughput["total_token/s"]), 4),
    }

    if metrics["avg_input_tokens"] is not None:
        message[_AVERAGE_INPUT_TOKENS] = round(
            float(metrics["avg_input_tokens"]), 4
        )

    if metrics["avg_output_tokens"] is not None:
        message[_AVERAGE_OUTPUT_TOKENS] = round(
            float(metrics["avg_output_tokens"]), 4
        )

    if latency["mean"] is not None:
        message[_AVERAGE_LATENCY] = round(float(latency["mean"]), 4)

    if ttft["mean"] is not None:
        message[_AVERAGE_TTFT] = round(float(ttft["mean"]) * 1000, 2)

    if tpot["mean"] is not None:
        avg_tpot_ms = round(float(tpot["mean"]) * 1000, 2)
        message[_AVERAGE_TPOT] = avg_tpot_ms
        message[_AVERAGE_ITL] = avg_tpot_ms

    return message


def wandb_timestamp() -> str:
    """Return ``YYYYMMDD_HHMMSS`` for W&B names / groups."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def wandb_run_base(config: BenchConfig) -> str:
    """Name/group base: ``--wandb-run-name`` if set, else ``{model}_{time}``."""
    run_name = config.wandb.run_name.strip()
    if run_name:
        return run_name
    return f"{config.target.model}_{wandb_timestamp()}"


def format_wandb_run_name(base: str, config_label: Optional[str] = None) -> str:
    """``{base}`` or ``{base}_{config}`` (config uses ``-`` separators)."""
    if config_label is None:
        return base
    label = config_label.strip().replace("_", "-")
    if not label:
        return base
    return f"{base}_{label}"


def compose_wandb_config_label(*parts: Optional[str]) -> Optional[str]:
    """Join non-empty config label parts with ``-``."""
    labels = [
        part.strip().replace("_", "-")
        for part in parts
        if part and part.strip()
    ]
    if not labels:
        return None
    return "-".join(labels)


class WandbLogger:
    """Optional W&B session: init, progressive log, final log, finish."""

    def __init__(self) -> None:
        self._enabled = False
        self._running: Optional[_RunningAverages] = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(
        self,
        config: BenchConfig,
        *,
        output_dir: str,
        parallel: int,
        rate: float,
        name_suffix: Optional[str] = None,
        group: Optional[str] = None,
    ) -> None:
        """Initialize W&B; no-op when ``config.wandb`` is off.

        Naming:
        - Single-point: run name ``{model}_{time}`` (no ``group``).
        - Multi-run: ``group`` is ``{model}_{time}``; each run is
          ``{group}_{config}`` (config segments joined with ``-``).
        """
        wandb_config: WandbConfig = config.wandb
        if not wandb_config.enabled:
            return

        os.environ["WANDB_SILENT"] = "true"
        os.environ["WANDB_DIR"] = output_dir
        if group:
            base = group
        else:
            base = wandb_run_base(config)
        name = format_wandb_run_name(base, name_suffix)
        init_kwargs: dict[str, Any] = {
            "project": wandb_config.project,
            "name": name,
            "config": config.to_dict(),
            "dir": output_dir,
            "settings": wandb.Settings(
                x_stats_sampling_interval=_SYSTEM_STATS_INTERVAL_S
            ),
        }
        if group:
            init_kwargs["group"] = group
        if wandb_config.entity:
            init_kwargs["entity"] = wandb_config.entity
        wandb.init(**init_kwargs)
        self._enabled = True
        self._running = _RunningAverages(
            concurrency=parallel,
            rate=rate,
        )
        logger.info(
            "W&B logging enabled: project=%s name=%s group=%s",
            wandb_config.project,
            name,
            group if group else "-",
        )

    def log_result(self, result: dict[str, Any]) -> None:
        """Log one request into the progressive W&B step series."""
        if not self._enabled or self._running is None:
            return
        with self._lock:
            wandb.log(self._running.update(result))

    def log_metrics(self, metrics: dict[str, Any]) -> None:
        """Final summary log using Foretoken aggregated metrics."""
        if not self._enabled:
            return
        with self._lock:
            wandb.log(metrics_to_wandb_message(metrics))

    def finish(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            wandb.finish()
            self._enabled = False
            self._running = None
