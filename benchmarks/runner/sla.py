# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""SLA concurrency search: probe load points until the SLA bound is found."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from math import nan
from typing import Any, Callable

from evalscope.perf.arguments import Arguments
from evalscope.perf.sla.sla_criterion import SLACriterionBase
from evalscope.perf.sla.sla_run import SLAAutoTuner, parse_sla_params
from evalscope.perf.sla import sla_run as evalscope_sla_run
from evalscope.perf.utils.perf_constants import Metrics
from evalscope.perf.utils.perf_models import (
    BenchmarkSummary,
    PercentileResult,
    PercentileRow,
)

from benchmarks.logger.wandb import wandb_run_base
from benchmarks.report.summary import log_sla_search_start, log_sla_summary
from benchmarks.runner.base import Runner
from benchmarks.runner.run_benchmark import RunBenchmark
from benchmarks.runner.run_spec import RunSpec
from benchmarks.storage.result_writer import ResultWriter

logger = logging.getLogger(__name__)


class SLARunner(Runner):
    """Find the maximum concurrency that satisfies the configured SLA.

    Owns the experiment-root writer; each probe owns its own client, child
    writer, and W&B logger via ``RunBenchmark``.
    """

    async def run(self) -> dict[str, Any]:
        """Run the synchronous SLA search off the event loop."""
        return await asyncio.to_thread(self._run_sync)

    def _run_sync(self) -> dict[str, Any]:
        """Drive SLA search; each probe runs in its own event loop."""
        config = self.config
        sla = config.sla
        writer = ResultWriter(root_dir=config.output.output_dir)
        group = wandb_run_base(config) if config.output.includes("wandb") else None
        if config.dataset.prompt:
            dataset = "prompt"
        else:
            dataset = config.dataset.dataset[0]
        args = Arguments.model_construct(
            model=config.endpoint.model,
            model_id=config.endpoint.model,
            dataset=dataset,
            stream=config.generation.stream,
            parallel=config.load.parallel,
            dataset_offset=config.dataset.dataset_offset,
            outputs_dir=writer.output_dir,
            sla_params=sla.params,
            sla_lower_bound=sla.lower_bound,
            sla_upper_bound=sla.upper_bound,
            sla_number_multiplier=sla.number_multiplier,
            sla_num_runs=config.num_runs,
        )

        def run_point(args: Arguments, output_dir: str) -> dict[str, Any]:
            """Run one concurrency probe and return SLA-shaped metrics."""
            point_config = replace(
                config,
                load=replace(
                    config.load,
                    parallel=args.parallel,
                    number=args.number,
                    rate=args.rate,
                ),
                dataset=(
                    replace(config.dataset, dataset_offset=args.dataset_offset)
                    if config.dataset.dataset == ["random"]
                    else config.dataset
                ),
            )
            label = os.path.basename(output_dir)
            result = asyncio.run(
                RunBenchmark(
                    RunSpec(
                        config=point_config,
                        label=label,
                        output_dir=output_dir,
                        wandb_group=group,
                    )
                ).run()
            )
            if result["metrics"]["failed_num"]:
                logger.warning(
                    "%s: %s requests failed; this concurrency cannot satisfy SLA",
                    label,
                    result["metrics"]["failed_num"],
                )
            return {label: self._probe_result(result["metrics"])}

        results = self._auto_tune(args, run_point, sla.params)
        if not results:
            raise ValueError("SLA search produced no probe results")
        succeed = max(
            int(result["metrics"][Metrics.SUCCEED_REQUESTS])
            for result in results.values()
        )
        if succeed <= 0:
            raise ValueError("SLA search completed without successful requests")
        return {
            "mode": "sla",
            "metrics": {"success_num": succeed},
            "results": results,
            "output_dir": writer.output_dir,
        }

    def _auto_tune(
        self,
        args: Arguments,
        run_point: Callable[[Arguments, str], dict[str, Any]],
        display_params: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Run evalscope search with Foretoken AND/OR checks and summary logging."""
        tuner = SLAAutoTuner(args, run_point)
        sla_params = parse_sla_params(args.sla_params)
        log_sla_search_start(
            variable=tuner.sla_variable,
            lower_bound=tuner.lower_bound,
            upper_bound=tuner.upper_bound,
            params=display_params,
        )
        original = evalscope_sla_run.check_sla
        evalscope_sla_run.check_sla = self._check_sla
        try:
            # Foretoken SLA search tunes concurrency only; bounds checked in BenchConfig.
            tuner._tune_constraint(int(args.parallel), sla_params, combined=True)
        finally:
            evalscope_sla_run.check_sla = original
        results = tuner._save_summary()
        log_sla_summary(
            params=display_params,
            variable=tuner.sla_variable,
            sla_results_table=tuner.sla_results_table,
            output_dir=args.outputs_dir,
        )
        return results

    @staticmethod
    def _probe_result(metrics: dict[str, Any]) -> dict[str, Any]:
        """Build one probe's BenchmarkSummary and percentiles for SLA averaging."""
        # Keep latency / TTFT / TPOT in seconds to match Foretoken SLA limits.
        valid = metrics["failed_num"] == 0
        timings = {
            name: {
                statistic: nan if not valid else float(value)
                for statistic, value in metrics[name].items()
            }
            for name in ("latency", "ttft", "tpot")
        }
        averages = {
            f"avg_{name}": stats["mean"] for name, stats in timings.items()
        }
        for key in ("avg_input_tokens", "avg_output_tokens"):
            averages[key] = nan if not valid else metrics[key]
        throughput = metrics["throughput"]
        summary = BenchmarkSummary(
            time_taken=metrics["benchmark_time"],
            concurrency=metrics["parallel"],
            request_rate=metrics["rate"],
            total_requests=metrics["request_num"],
            succeed_requests=metrics["success_num"],
            failed_requests=metrics["failed_num"],
            stream_requests=metrics["request_num"] if metrics["stream"] else 0,
            non_stream_requests=0 if metrics["stream"] else metrics["request_num"],
            request_throughput=throughput["requests_per_second"],
            output_token_throughput=throughput["generation_tokens_per_second"],
            total_token_throughput=throughput["total_tokens_per_second"],
            input_token_throughput=throughput["prompt_tokens_per_second"],
            **averages,
        )
        percentiles = PercentileResult(
            rows=[
                PercentileRow(
                    percentile=f"{percentile}%",
                    **{
                        name: stats[f"p{percentile}"]
                        for name, stats in timings.items()
                    },
                )
                for percentile in (50, 95, 99)
            ]
        )
        return {"metrics": summary, "percentiles": percentiles}

    @staticmethod
    def _check_sla(
        results: dict[str, Any],
        sla_criteria: list[dict[str, SLACriterionBase]],
        selector: str | None = None,
    ) -> bool:
        """Validate one probe: AND within a group, short-circuit OR across groups."""
        # SLAAutoTuner averages probes into plain dicts before calling check_sla.
        prefix = f"[{selector}] " if selector else ""
        summary = BenchmarkSummary.from_dict(results["metrics"])
        if summary.success_rate < 100.0:
            logger.warning(
                "%sSLA Check: Success Rate = %.2f%% | Expect 100%% | FAILED",
                prefix,
                summary.success_rate,
            )
            return False

        percentiles = PercentileResult.from_transposed(results["percentiles"])
        values = {
            "avg_latency": summary.avg_latency,
            "avg_ttft": summary.avg_ttft,
            "avg_tpot": summary.avg_tpot,
            "rps": summary.request_throughput,
            "tps": summary.output_token_throughput,
            "p99_latency": percentiles.get_p("99%", "latency"),
            "p99_ttft": percentiles.get_p("99%", "ttft"),
            "p99_tpot": percentiles.get_p("99%", "tpot"),
            "p95_latency": percentiles.get_p("95%", "latency"),
            "p95_ttft": percentiles.get_p("95%", "ttft"),
            "p95_tpot": percentiles.get_p("95%", "tpot"),
            "p50_ttft": percentiles.get_p("50%", "ttft"),
            "p50_tpot": percentiles.get_p("50%", "tpot"),
        }

        for index, criteria_group in enumerate(sla_criteria):
            group_passed = True
            for metric, criterion in criteria_group.items():
                value = values[metric]
                passed = criterion.validate(value)
                logger.info(
                    "%sSLA Rule %s Check: %s = %.4f | Expect %s | %s",
                    prefix,
                    index + 1,
                    metric,
                    value,
                    criterion.format_cond(""),
                    "PASSED" if passed else "FAILED",
                )
                if not passed:
                    group_passed = False
                    break
            if group_passed:
                return True
        return False
