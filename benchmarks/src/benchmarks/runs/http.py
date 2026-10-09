# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own preparation, warmup, measurement, observation, and publication for HTTP workloads."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.datasets.workload import HttpWorkload, RequestEpisode
from benchmarks.integrations.openai import OpenAILoadClient
from benchmarks.model_service import ModelService
from benchmarks.profiling.capture import BenchmarkProfile
from benchmarks.results.metrics import (
    RequestMeasurement,
    percentile_summary,
    summarize_measurement_groups,
    summarize_measurements,
)
from benchmarks.results.output import (
    BenchmarkRun,
    ResultOutputs,
    build_benchmark_run_record,
    request_measurement_record,
    resolved_load_record,
    write_json,
)
from benchmarks.runs.scheduling import run_schedule

logger = logging.getLogger(__name__)


@dataclass
class EpisodeResult:
    """Responses and conversation completion produced by one admitted episode."""

    records: list[dict[str, Any]]
    conversation_attempted: bool = False
    conversation_completed: bool = False


def request_measurement(record: dict[str, Any], origin: float) -> RequestMeasurement:
    """Project an HTTP observation onto its phase clock for common metrics and sinks."""
    return RequestMeasurement(
        started_at=record["started_at"] - origin,
        ttft=record["ttft"],
        latency=record["latency"],
        tpot=record["tpot"],
        itl_samples=tuple(record["inter_token_latencies"]),
        input_tokens=record["input_tokens"],
        output_tokens=record["output_tokens"],
        cached_input_tokens=record["cached_input_tokens"],
        succeeded=record["success"],
        conversation_id=record["conversation_id"],
        turn=record["turn"],
        status_code=record["status_code"],
        error_message=record["error"],
        dataset=record["dataset"],
        model=record["model"],
        priority=record["priority"],
        request_class=record["request_class"],
        target_output_tokens=record["target_output_tokens"],
    )


class HttpBenchmark:
    """Run one workload point with one client and isolated warmup and measurement phases."""

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
        *,
        label: str = "",
        output_dir: str | None = None,
        wandb_group: str | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.service = service
        self.label = label
        self.output_dir = output_dir
        self.wandb_group = wandb_group

    async def _execute_episode(
        self,
        client: OpenAILoadClient,
        workload: HttpWorkload,
        index: int,
        episode: RequestEpisode,
        scheduled_at: float,
        deadline: float | None,
        profile: BenchmarkProfile | None,
    ) -> EpisodeResult:
        """Advance one conversation, retaining recorded or generated history and request failures."""
        result = EpisodeResult([])
        context: list[dict[str, Any]] = []
        generated_history = workload.dataset.conversation_history == "generated"
        for turn_index, turn in enumerate(episode.turns):
            if deadline is not None and time.perf_counter() >= deadline:
                break
            if profile is not None:
                await profile.before_request()
            request = turn.request
            if episode.conversation:
                request = replace(
                    request,
                    body={
                        **request.body,
                        "messages": context + request.body["messages"],
                    },
                )
                result.conversation_attempted = True
            response = await client.send(request)
            if (
                generated_history
                and turn_index < len(episode.turns) - 1
                and response["tool_calls"]
            ):
                response.update(
                    success=False,
                    error="Model requested tool execution before the next turn; a harness is required",
                )
            if profile is not None:
                profile.response_received(response["success"])
            metadata = episode.task.metadata
            response.update(
                conversation_id=f"{episode.task.id}:{index}"
                if episode.conversation
                else None,
                turn=turn_index if episode.conversation else None,
                dataset=metadata.get("_dataset")
                or (
                    workload.dataset.dataset_selectors[0]
                    if workload.dataset.dataset_selectors
                    else None
                ),
                priority=metadata.get("priority"),
                request_class=metadata.get("request_class"),
            )
            if episode.trace is not None:
                event = episode.trace
                delay = max(0.0, response["started_at"] - scheduled_at)
                response.update(
                    source_index=event.source_row_index,
                    conversation_id=event.conversation_id,
                    trace_timestamp_s=event.timestamp_seconds,
                    trace_offset_s=event.timestamp_seconds - workload.trace_start,
                    trace_input_length=event.input_tokens,
                    trace_hash_block_count=len(event.hash_ids)
                    if event.hash_ids is not None
                    else None,
                    payload_source=event.request_origin,
                    replay_delay=delay,
                    trace_e2e_latency=delay + response["latency"],
                    trace_e2e_ttft=delay + response["ttft"]
                    if response["ttft"] is not None
                    else None,
                )
            result.records.append(response)
            if not response["success"]:
                break
            if episode.conversation:
                context.extend(turn.request.body["messages"])
                if generated_history:
                    context.append(
                        {"role": "assistant", "content": response["generated_text"]}
                    )
                elif turn.answer is not None:
                    context.append(turn.answer)
                result.conversation_completed = turn_index == len(episode.turns) - 1
        return result

    def _record(self, workload: HttpWorkload) -> dict[str, Any]:
        """Record resolved load coordinates without exposing execution machinery as configuration."""
        load = resolved_load_record(self.benchmark)
        load["num_prompts"] = workload.request_count
        trace = self.benchmark.trace
        if workload.trace_events is not None:
            load.update(
                request_rate=-1.0, duration=trace.duration_seconds, open_loop=False
            )
        record = build_benchmark_run_record(
            self.benchmark,
            self.service,
            "arrival_trace" if workload.trace_events is not None else "http_load",
            load,
        )
        if self.benchmark.is_multi_turn:
            record["multi_turn"] = bool(workload.scripts)
        if workload.dataset_tasks is not None:
            record["datasets"] = list(workload.dataset_tasks)
        if workload.trace_events is not None:
            record.update(
                dataset=f"trace={trace.trace_selector}",
                trace_path=trace.trace_selector,
                payload_dataset=workload.dataset.dataset_selectors[0],
                trace_start=trace.start_offset_seconds,
                trace_duration=trace.duration_seconds,
                trace_synthetic_prefix_reuse=trace.synthetic_prefix_reuse,
                trace_format=workload.trace_format,
                payload_source=workload.request_origin,
            )
        return record

    async def _run(self) -> BenchmarkRun:
        """Prepare requests, drain independent warmup, then observe and publish the formal phase."""
        benchmark = self.benchmark
        with ResultOutputs(
            benchmark,
            self.service,
            label=self.label,
            output_dir=self.output_dir,
            wandb_group=self.wandb_group,
        ) as outputs:
            workload = HttpWorkload(benchmark, self.service)
            actual_models = {
                workload.builder.model_for(task.metadata) for task in workload.tasks
            } or {workload.builder.model_for({})}
            self.service = self.service.select_models(actual_models)
            outputs.service = self.service
            measured_episodes = workload.episodes()
            if workload.request_count is not None:
                measured_episodes = list(measured_episodes)
            record = self._record(workload)
            outputs.open(record, start_observers=False)
            async with OpenAILoadClient(
                benchmark,
                self.service,
                max_connections=benchmark.load.max_concurrency
                if benchmark.load.max_concurrency > 0
                else None,
            ) as client:

                async def execute(index, episode, scheduled_at, deadline):
                    return await self._execute_episode(
                        client, workload, index, episode, scheduled_at, deadline, None
                    )

                if benchmark.load.warmup_requests:
                    logger.info(
                        "Warmup: %d HTTP requests", benchmark.load.warmup_requests
                    )
                    warmup_load = replace(
                        benchmark.load, arrival_rate=-1.0, duration_seconds=None
                    )
                    warmup, duration, origin = await run_schedule(
                        list(workload.episodes(warmup=True)),
                        execute,
                        warmup_load,
                        seed=workload.dataset.random_seed,
                    )
                    records = sorted(
                        (row for item in warmup for row in item.records),
                        key=lambda row: row["started_at"],
                    )
                    measurements = [request_measurement(row, origin) for row in records]
                    outputs.record_http_warmup(
                        measurements,
                        duration=duration,
                        stream=benchmark.generation.stream,
                        arrival_rate=-1.0,
                        concurrency=benchmark.load.max_concurrency,
                    )
                    failed = next((row for row in records if not row["success"]), None)
                    if failed is not None:
                        raise ValueError(f"Warmup failed: {failed['error']}")

                outputs.start_observers()
                profile = outputs.create_profile()
                with profile if profile is not None else nullcontext():
                    async def measure(index, episode, scheduled_at, deadline):
                        return await self._execute_episode(
                            client,
                            workload,
                            index,
                            episode,
                            scheduled_at,
                            deadline,
                            profile,
                        )

                    logger.info(
                        "Measurement: %s HTTP requests",
                        workload.request_count or "duration-bounded",
                    )
                    episodes, duration, origin = await run_schedule(
                        measured_episodes,
                        measure,
                        benchmark.load,
                        seed=workload.dataset.random_seed,
                        trace_start=workload.trace_start
                        if workload.trace_events is not None
                        else None,
                        before_first=profile.start if profile is not None else None,
                    )
            records = sorted(
                (row for item in episodes for row in item.records),
                key=lambda row: row["started_at"],
            )
            measurements = [request_measurement(row, origin) for row in records]
            criteria = benchmark.slo.params[0] if benchmark.slo.params else None
            metrics = summarize_measurements(
                measurements,
                total_time=duration,
                stream=benchmark.generation.stream,
                arrival_rate=benchmark.load.arrival_rate,
                request_count=workload.request_count
                if workload.request_count is not None
                else len(measurements),
                reported_concurrency=benchmark.load.max_concurrency,
                gpu_count=self.service.gpu_count
                if {item.model for item in measurements} == {self.service.model}
                else None,
                include_normalized_throughput=workload.trace_events is None,
                slo_criteria=criteria,
            )
            metrics.update(
                summarize_measurement_groups(
                    measurements,
                    total_time=duration,
                    stream=benchmark.generation.stream,
                    arrival_rate=benchmark.load.arrival_rate,
                    reported_concurrency=benchmark.load.max_concurrency,
                    slo_criteria=criteria,
                    include_single_dataset=workload.dataset_tasks is not None,
                )
            )
            attempted = sum(item.conversation_attempted for item in episodes)
            if attempted:
                metrics.update(
                    multi_turn=True,
                    conversation={
                        "attempted_num": attempted,
                        "completed_num": sum(
                            item.conversation_completed for item in episodes
                        ),
                        "request_num": len(measurements),
                        "max_turns": workload.dataset.max_turns,
                        "avg_turn_requests": len(measurements) / attempted,
                        "attempted_conversations_per_second": attempted / duration
                        if duration
                        else 0.0,
                    },
                )
            artifacts = (
                {"profile": Path(outputs.execution_dir) / "profile.json"}
                if profile is not None
                else {}
            )
            if workload.trace_events is not None:
                for name in ("replay_delay", "trace_e2e_ttft", "trace_e2e_latency"):
                    metrics[name] = percentile_summary(
                        [
                            row[name]
                            for row in records
                            if row["success"] and row[name] is not None
                        ]
                    )
                slo = metrics.get("slo") or {}
                for row, met in zip(records, slo.get("request_slo_met") or []):
                    row.update(slo_met=met, slo_target=slo["request_criteria"])
                artifacts["raw_output"] = write_json(
                    outputs.execution_dir,
                    "raw_output.json",
                    [
                        {
                            **{
                                key: value
                                for key, value in row.items()
                                if key != "started_at"
                            },
                            **request_measurement_record(
                                item, stream=benchmark.generation.stream
                            ),
                        }
                        for row, item in zip(records, measurements)
                    ],
                )
            run = BenchmarkRun(
                record=record,
                metrics=metrics,
                measurements=measurements,
                artifacts=artifacts,
                time_origin=origin,
            )
            outputs.publish(run)
            return run

    def run(self) -> BenchmarkRun:
        """Execute one measurement point for the CLI, sweep, or SLO search."""
        return asyncio.run(self._run())
