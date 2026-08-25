# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Run benchmarks over multiple datasets and merge metrics."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime
from typing import Any

from benchmarks.config import allocate_dataset_counts
from benchmarks.metrics.aggregator import merge_raw_outputs
from benchmarks.report.summary import log_summary
from benchmarks.runner.base import Runner
from benchmarks.workload.loader import load_requests

logger = logging.getLogger(__name__)


def _child_dir_name(index: int, source: str) -> str:
    safe = re.sub(r"[^\w.\-]+", "_", source).strip("_")
    return f"{index:02d}_{safe or 'dataset'}"


class MultiDatasetRunner(Runner):
    """Run each dataset sequentially, then merge raw outputs and reaggregate.

    W&B: one experiment is one ``group``; each dataset is its own ``run``.
    Merged metrics are saved locally only (no extra W&B run).
    """

    async def run(self) -> dict[str, Any]:
        load = self.default_load()
        sources = list(self.config.dataset.dataset)
        total = int(load["number"])
        counts = allocate_dataset_counts(total, len(sources))
        writer = self.make_writer()
        run_config = self.build_run_config("multi_dataset", load)
        run_config["datasets"] = sources
        run_config["dataset_numbers"] = counts

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        wandb_group = (
            self.config.wandb.run_name or f"{self.config.endpoint.model}_{stamp}"
        )

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
            requests = load_requests(self.config, source=source, number=count)
            child_name = _child_dir_name(index, source)
            child = writer.child(child_name)
            child_config = {
                key: value
                for key, value in run_config.items()
                if key not in ("datasets", "dataset_numbers")
            }
            child_config.update(
                {
                    "dataset": source,
                    "number": count,
                    "resolved": {
                        **run_config["resolved"],
                        "number": count,
                    },
                }
            )
            per_dataset_config = replace(
                self.config,
                dataset=replace(self.config.dataset, dataset=[source]),
            )
            wandb_logger = self.make_wandb_logger(
                child,
                load,
                name_suffix=child_name,
                group=wandb_group,
                config=per_dataset_config,
            )
            try:
                raw_output = await self.dispatch(
                    self.make_client(),
                    requests,
                    parallel=load["parallel"],
                    rate=load["rate"],
                    open_loop=load["open_loop"],
                )
                metrics = self.aggregate_metrics(
                    raw_output,
                    rate=load["rate"],
                    number=count,
                    resolved_parallel=load["resolved_parallel"],
                )
                if not self.config.output.includes("quiet"):
                    log_summary(child_config, metrics)
                child.save_json(
                    "config.json",
                    {**per_dataset_config.to_dict(), **child_config},
                )
                child.save_json("raw_output.json", raw_output["results"])
                child.save_json("metrics.json", metrics)
                if wandb_logger.enabled:
                    wandb_logger.log_metrics(metrics)
                raw_outputs.append(raw_output)
            except Exception:
                wandb_logger.finish()
                raise
            else:
                wandb_logger.finish()

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
