# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Run benchmarks over multiple datasets and merge metrics."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from typing import Any

from benchmarks.config import allocate_dataset_counts
from benchmarks.logger.wandb import wandb_run_base
from benchmarks.metrics.aggregator import merge_raw_outputs
from benchmarks.runner.base import Runner
from benchmarks.runner.run_benchmark import RunBenchmark
from benchmarks.runner.run_spec import RunSpec

logger = logging.getLogger(__name__)


def _child_dir_name(index: int, source: str) -> str:
    safe = re.sub(r"[^\w.\-]+", "_", source).strip("_")
    return f"{index:02d}_{safe or 'dataset'}"


class MultiDatasetRunner(Runner):
    """Allocate sources, run each point via ``RunBenchmark``, merge raw output.

    Owns the experiment-root writer. Merged metrics are saved locally only
    (no extra W&B run); each source is its own W&B run under one group.
    """

    async def run(self) -> dict[str, Any]:
        load = self.default_load()
        sources = list(self.config.dataset.dataset)
        total = int(load["number"])
        counts = allocate_dataset_counts(total, len(sources))
        writer = self.create_writer()
        run_config = self.build_run_config("multi_dataset", load)
        run_config["datasets"] = sources
        run_config["dataset_numbers"] = counts

        wandb_enabled = self.config.output.includes("wandb")
        wandb_group = wandb_run_base(self.config) if wandb_enabled else None

        raw_outputs: list[dict[str, Any]] = []
        for index, (source, count) in enumerate(zip(sources, counts)):
            if count == 0:
                logger.info(
                    "Skipping dataset %s (allocated 0 of total %s)",
                    source,
                    total,
                )
                continue

            logger.info(
                "Dataset %s/%s: %s (number=%s)",
                index + 1,
                len(sources),
                source,
                count,
            )
            child_name = _child_dir_name(index, source)
            child_config = replace(
                self.config,
                dataset=replace(self.config.dataset, dataset=[source]),
                load=replace(self.config.load, number=count),
            )
            result = await RunBenchmark(
                RunSpec(
                    config=child_config,
                    label=child_name,
                    output_dir=os.path.join(writer.output_dir, child_name),
                    wandb_group=wandb_group,
                )
            ).run()
            raw_outputs.append(result["raw"])

        if not raw_outputs:
            raise ValueError(
                f"No requests dispatched for datasets={sources} "
                f"with total number={total}"
            )

        merged = merge_raw_outputs(raw_outputs)
        metrics = self.aggregate_metrics(
            merged,
            rate=load["rate"],
            number=total,
            resolved_parallel=load["resolved_parallel"],
        )
        self.save_results(writer, run_config, merged, metrics)

        return {
            "mode": "multi_dataset",
            "metrics": metrics,
            "output_dir": writer.output_dir,
            "datasets": sources,
            "dataset_numbers": counts,
            "wandb_group": wandb_group,
        }
