# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own preparation, warmup, measurement, observation, and publication for HTTP workloads."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.datasets.workload import HttpWorkload, RequestEpisode
from benchmarks.integrations.openai import OpenAILoadClient
from benchmarks.model_service import ModelService
from benchmarks.profiling.capture import BenchmarkProfile
from benchmarks.results.itl import ITLStatistics
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
from benchmarks.results.progress import HttpProgress
from benchmarks.runs.scheduling import run_schedule

logger = logging.getLogger(__name__)


@dataclass
class EpisodeResult:
    """Responses and conversation completion produced by one admitted episode."""

    records: list[dict[str, Any]]
    planned_num: int
    skipped_dependency_num: int = 0
    conversation_attempted: bool = False
    conversation_completed: bool = False


def request_measurement(record: dict[str, Any], origin: float) -> RequestMeasurement:
    """Project an HTTP observation onto its phase clock for common metrics and sinks."""
    return RequestMeasurement(
        started_at=record["started_at"] - origin,
        ttft=record["ttft"],
        latency=record["latency"],
        tpot=record["tpot"],
        itl_summary=record["itl_summary"],
        request_id=record["request_id"],
        phase=record["phase"],
        source_id=record["source_id"],
        scheduled_at=record["scheduled_at"] - origin
        if record["scheduled_at"] is not None
        else None,
        send_delay=record["send_delay"],
        finish_reason=record["finish_reason"],
        response_id=record["response_id"],
        response_model=record["response_model"],
        content_characters=record["content_characters"],
        reasoning_characters=record["reasoning_characters"],
        transport_completed=record["transport_completed"],
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
        outputs: ResultOutputs | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.service = service
        self.label = label
        self.output_dir = output_dir
        self.wandb_group = wandb_group
        self.outputs = outputs

    async def _execute_episode(
        self,
        client: OpenAILoadClient,
        workload: HttpWorkload,
        index: int,
        episode: RequestEpisode,
        scheduled_at: float,
        deadline: float | None,
        profile: BenchmarkProfile | None,
        phase: str,
        itl: ITLStatistics,
        group_itl: dict[str, dict[str, ITLStatistics]],
        record_completed: Callable[[dict[str, Any]], None],
        progress: HttpProgress | None = None,
    ) -> EpisodeResult:
        """Advance one conversation, retaining recorded or generated history and request failures."""
        result = EpisodeResult([], planned_num=len(episode.turns))
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
            response = await client.send(
                request,
                request_id=f"{phase}:{index}:{turn_index}",
                phase=phase,
                source_id=episode.task.id,
                conversation_id=f"{episode.task.id}:{index}"
                if episode.conversation
                else None,
                turn=turn_index if episode.conversation else None,
                retain_history=generated_history and episode.conversation,
                on_start=progress.request_started if progress is not None else None,
            )
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
            planned = (
                phase == "measurement"
                and turn_index == 0
                and (episode.trace is not None or self.benchmark.load.arrival_rate > 0)
            )
            response["scheduled_at"] = scheduled_at if planned else None
            response["send_delay"] = (
                max(0.0, response["started_at"] - scheduled_at) if planned else None
            )
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
            request_itl = response.pop("itl")
            response["itl_summary"] = request_itl.summary()
            if response["success"]:
                itl.merge(request_itl)
                for field, dimension in (
                    ("dataset", "datasets"),
                    ("model", "models"),
                    ("request_class", "request_classes"),
                ):
                    name = response.get(field)
                    if name is not None:
                        group_itl.setdefault(dimension, {}).setdefault(
                            name, ITLStatistics()
                        ).merge(request_itl)
            generated_text = response.pop("generated_text")
            response.pop("tool_calls")
            result.records.append(response)
            record_completed(response)
            if progress is not None:
                progress.observe(request_measurement(response, progress.origin))
            if not response["success"]:
                result.skipped_dependency_num = len(episode.turns) - turn_index - 1
                break
            if episode.conversation:
                context.extend(turn.request.body["messages"])
                if generated_history:
                    context.append({"role": "assistant", "content": generated_text})
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
        context = nullcontext(self.outputs) if self.outputs is not None else ResultOutputs(
            benchmark, self.service, label=self.label,
            output_dir=self.output_dir, wandb_group=self.wandb_group,
        )
        with context as outputs:
            workload = HttpWorkload(benchmark, self.service)
            actual_models = {
                workload.builder.model_for(task.metadata) for task in workload.tasks
            } or {workload.builder.model_for({})}
            self.service = self.service.select_models(actual_models)
            outputs.service = self.service
            measured_episodes = workload.episodes()
            if workload.request_count is not None:
                measured_episodes = list(measured_episodes)
            planned_num = (
                sum(len(episode.turns) for episode in measured_episodes)
                if isinstance(measured_episodes, list) else None
            )
            record = self._record(workload)
            record["planned_num"] = planned_num
            outputs.open(record, start_observers=False)
            async with OpenAILoadClient(
                benchmark,
                self.service,
                response_writer=outputs.create_response_writer(),
                max_connections=benchmark.load.max_concurrency
                if benchmark.load.max_concurrency > 0
                else None,
            ) as client:
                phase_origin = 0.0
                progress = None

                def phase_started(origin: float) -> None:
                    """Align durable records and live series with the scheduler's phase clock."""
                    nonlocal phase_origin
                    phase_origin = origin
                    if progress is not None:
                        progress.start(origin)

                def record_completed(response: dict[str, Any]) -> None:
                    """Flush a completed request before live publication or another turn."""
                    item = request_measurement(response, phase_origin)
                    writer.write({
                        **{key: value for key, value in response.items() if key != "started_at"},
                        **request_measurement_record(item, stream=benchmark.generation.stream),
                    })

                warmup_itl = ITLStatistics()
                warmup_groups: dict[str, dict[str, ITLStatistics]] = {}

                async def execute(index, episode, scheduled_at, deadline):
                    return await self._execute_episode(
                        client,
                        workload,
                        index,
                        episode,
                        scheduled_at,
                        deadline,
                        None,
                        "warmup",
                        warmup_itl,
                        warmup_groups,
                        record_completed,
                    )

                if benchmark.load.warmup_requests:
                    writer = outputs.create_request_writer("warmup_raw_output")
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
                        on_start=phase_started,
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
                        itl_summary=warmup_itl.summary(),
                    )
                    failed = next((row for row in records if not row["success"]), None)
                    if failed is not None:
                        raise ValueError(f"Warmup failed: {failed['error']}")

                writer = outputs.create_request_writer("raw_output")
                measured_itl = ITLStatistics()
                measured_groups: dict[str, dict[str, ITLStatistics]] = {}
                outputs.start_observers()
                progress = outputs.http_progress(
                    stream=benchmark.generation.stream,
                    criteria=benchmark.slo.params[0] if benchmark.slo.params else None,
                )
                profile = outputs.create_profile()
                with profile if profile is not None else nullcontext():

                    def measurement_started(origin: float) -> None:
                        """Confirm formal dispatch after warmup and capture readiness."""
                        outputs.start_measurement()
                        phase_started(origin)

                    async def measure(index, episode, scheduled_at, deadline):
                        return await self._execute_episode(
                            client,
                            workload,
                            index,
                            episode,
                            scheduled_at,
                            deadline,
                            profile,
                            "measurement",
                            measured_itl,
                            measured_groups,
                            record_completed,
                            progress,
                        )

                    logger.info(
                        "Measurement: %s HTTP requests",
                        workload.request_count or "duration-bounded",
                    )

                    def measured_run(episodes: list[EpisodeResult], duration: float, origin: float) -> BenchmarkRun:
                        """Retain the completed window before progress, profile, or transport finalization."""
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
                            itl_summary=measured_itl.summary(),
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
                                itl_summaries={
                                    dimension: {
                                        name: stats.summary() for name, stats in groups.items()
                                    }
                                    for dimension, groups in measured_groups.items()
                                },
                            )
                        )
                        # Duration-only sources have no finite plan; their skipped
                        # turns are counted only within admitted episodes.
                        admitted_num = sum(item.planned_num for item in episodes)
                        skipped_dependency = sum(item.skipped_dependency_num for item in episodes)
                        not_executed_num = (planned_num if planned_num is not None else admitted_num) - len(measurements)
                        metrics.update(
                            planned_num=planned_num,
                            admitted_num=admitted_num,
                            not_executed_num=not_executed_num,
                            not_executed_reasons={
                                "failed_predecessor": skipped_dependency,
                                "duration": not_executed_num - skipped_dependency,
                            },
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
                        run = BenchmarkRun(
                            record=record,
                            metrics=metrics,
                            measurements=measurements,
                            artifacts=artifacts,
                            time_origin=origin,
                            exit_code=0 if metrics["success_num"] else 1,
                            http_series_published=False,
                        )
                        outputs.save_measurement(run)
                        if workload.trace_events is not None:
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
                                compact=True,
                            )
                        return run

                    async with asyncio.TaskGroup() as tasks:
                        sampler = tasks.create_task(progress.sample()) if progress is not None else None
                        try:
                            episodes, duration, origin = await run_schedule(
                                measured_episodes, measure, benchmark.load,
                                seed=workload.dataset.random_seed,
                                trace_start=workload.trace_start if workload.trace_events is not None else None,
                                before_first=profile.start if profile is not None else None,
                                on_start=measurement_started,
                            )
                            run = measured_run(episodes, duration, origin)
                        finally:
                            if sampler is not None:
                                sampler.cancel()
                    if progress is not None:
                        progress.emit(duration, final=True)
                        run.http_series_published = True
                        outputs.save_measurement(run)
            outputs.publish(run)
            return run

    def run(self) -> BenchmarkRun:
        """Execute one measurement point for the CLI, sweep, or SLO search."""
        return asyncio.run(self._run())
