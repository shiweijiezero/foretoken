# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Weights & Biases logging for final benchmark results."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Optional

import wandb

from benchmarks.config import BenchConfig, WandbConfig

logger = logging.getLogger(__name__)

_SYSTEM_STATS_INTERVAL_S = 1.0
_TIME_TAKEN = "Test Duration"
_CONCURRENCY = "Concurrency"
_REQUEST_RATE = "Request Rate"
_TOTAL_REQUESTS = "Total Requests"
_SUCCEED_REQUESTS = "Success Requests"
_FAILED_REQUESTS = "Failed Requests"
_REQUEST_THROUGHPUT = "Request Throughput"
_AVERAGE_LATENCY = "Avg Latency"
_AVERAGE_INPUT_TOKENS = "Avg Input Tokens"
_OUTPUT_TOKEN_THROUGHPUT = "Output Throughput"
_TOTAL_TOKEN_THROUGHPUT = "Total Throughput"
_AVERAGE_TTFT = "Avg TTFT"
_AVERAGE_TPOT = "Avg TPOT"
_AVERAGE_ITL = "Avg ITL"
_AVERAGE_OUTPUT_TOKENS = "Avg Output Tokens"


def metrics_to_wandb_message(metrics: dict[str, Any]) -> dict[str, Any]:
    """Map final Foretoken metrics to stable W&B chart keys."""
    throughput = metrics["throughput"]
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
    optional = (
        ("avg_input_tokens", _AVERAGE_INPUT_TOKENS, 1.0, 4),
        ("avg_output_tokens", _AVERAGE_OUTPUT_TOKENS, 1.0, 4),
        ("latency", _AVERAGE_LATENCY, 1.0, 4),
        ("ttft", _AVERAGE_TTFT, 1000.0, 2),
        ("tpot", _AVERAGE_TPOT, 1000.0, 2),
    )
    for source, destination, scale, digits in optional:
        value = metrics[source]
        if isinstance(value, dict):
            value = value["mean"]
        if value is not None:
            message[destination] = round(float(value) * scale, digits)
    if _AVERAGE_TPOT in message:
        message[_AVERAGE_ITL] = message[_AVERAGE_TPOT]
    return message


class WandbLogger:
    """Optional W&B session that publishes one final benchmark summary."""

    def __init__(self) -> None:
        self._active = False

    @property
    def enabled(self) -> bool:
        return self._active

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
        """Initialize W&B when selected as a result destination."""
        wandb_config: WandbConfig = config.wandb
        if not config.output.includes("wandb"):
            return

        os.makedirs(output_dir, exist_ok=True)
        os.environ["WANDB_SILENT"] = "true"
        os.environ["WANDB_DIR"] = output_dir
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = group or wandb_config.run_name or f"{config.endpoint.model}_{stamp}"
        name = f"{base}_{name_suffix}" if name_suffix else base
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
        self._active = True
        logger.info(
            "W&B logging enabled: project=%s name=%s group=%s concurrency=%s rate=%s",
            wandb_config.project,
            name,
            group or "-",
            parallel,
            rate,
        )

    def log_metrics(self, metrics: dict[str, Any]) -> None:
        """Publish the final aggregated benchmark metrics."""
        if self._active:
            wandb.log(metrics_to_wandb_message(metrics))

    def finish(self) -> None:
        if self._active:
            wandb.finish()
            self._active = False
