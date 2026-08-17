# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Command-line interface for direct and Kubernetes-managed benchmarks."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from dataclasses import dataclass

from benchmarks.config import (
    BenchConfig,
    DatasetConfig,
    EngineMetricsConfig,
    GenerationConfig,
    LoadConfig,
    OutputConfig,
    TargetConfig,
    WandbConfig,
)


@dataclass(frozen=True)
class RunCommand:
    base_url: str
    deploy: str
    model: str
    prompt: str
    datasets: tuple[str, ...]
    dataset_offset: int
    tokenizer_path: str
    min_prompt_length: int
    max_prompt_length: int
    prefix_length: int
    apply_chat_template: bool | None
    max_turns: int | None
    max_concurrency: int
    num_requests: int
    request_rate: float
    open_loop: bool
    timeout: int
    max_retries: int
    max_tokens: int
    temperature: float
    stream: bool
    name: str
    output_dir: str
    keep: bool
    benchmark_image: str
    storage_class: str
    results_size: str
    service_timeout: int
    job_timeout: int
    wandb: bool
    wandb_project: str
    wandb_entity: str
    wandb_run_name: str
    collect_engine_metrics: bool
    engine_metrics_url: str
    engine_metrics_interval: float
    run_id: str
    execution_context: str

    @property
    def is_managed(self) -> bool:
        return bool(self.deploy)

    @property
    def dataset_path(self) -> str:
        """Compatibility accessor for a single JSONL deploy workload."""
        return self.datasets[0] if len(self.datasets) == 1 else ""

    def bench_config(
        self,
        *,
        base_url: str,
        model: str,
        run_id: str,
        dataset_path: str | None = None,
        output_dir: str | None = None,
        execution_context: str | None = None,
    ) -> BenchConfig:
        datasets = self.datasets if dataset_path is None else ((dataset_path,) if dataset_path else ())
        return BenchConfig(
            target=TargetConfig(
                url=base_url,
                model=model,
                api_key=os.environ.get("OPENAI_API_KEY", ""),
                timeout=self.timeout,
                max_retries=self.max_retries,
            ),
            load=LoadConfig(
                parallel=[self.max_concurrency],
                number=[self.num_requests],
                rate=[self.request_rate],
                open_loop=self.open_loop,
            ),
            generation=GenerationConfig(
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                stream=self.stream,
            ),
            dataset=DatasetConfig(
                dataset=list(datasets),
                dataset_offset=self.dataset_offset,
                tokenizer_path=self.tokenizer_path,
                min_prompt_length=self.min_prompt_length,
                max_prompt_length=self.max_prompt_length,
                prefix_length=self.prefix_length,
                apply_chat_template=self.apply_chat_template,
                prompt=self.prompt,
                max_turns=self.max_turns,
            ),
            output=OutputConfig(
                outputs_dir=output_dir or self.output_dir,
                run_id=run_id,
                execution_context=execution_context or self.execution_context,
            ),
            wandb=WandbConfig(
                enabled=self.wandb,
                project=self.wandb_project,
                entity=self.wandb_entity,
                run_name=self.wandb_run_name or run_id,
            ),
            engine=EngineMetricsConfig(
                collect=self.collect_engine_metrics,
                url=self.engine_metrics_url,
                interval=self.engine_metrics_interval,
            ),
        )


@dataclass(frozen=True)
class CleanupCommand:
    run_id: str
    output_dir: str


Command = RunCommand | CleanupCommand


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base-url", help="Existing OpenAI-compatible API root, for example https://host/v1")
    source.add_argument("--deploy", help="YAML file or Kustomize directory to deploy before benchmarking")
    parser.add_argument("--model", default="", help="Model name; inferred from a single deployed ModelService")
    parser.add_argument("--timeout", type=int, default=300, help="Per-request timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=2, help="Retry count for transient OpenAI client failures")

    workload = parser.add_argument_group("workload")
    workload.add_argument("--prompt", help='Fixed prompt text; defaults to "Hello"')
    workload.add_argument("--dataset", action="append", default=[], help="Workload source: local JSONL, random, or Hugging Face id such as org/name:split; repeat for multiple sources")
    workload.add_argument("--dataset-path", dest="dataset", action="append", help="Compatibility alias for a local JSONL workload")
    workload.add_argument("--dataset-offset", type=int, default=0, help="Skip initial workload samples")
    workload.add_argument("--tokenizer-path", default="", help="Tokenizer path required by --dataset random")
    workload.add_argument("--min-prompt-length", type=int, default=0, help="Minimum random prompt length in tokens")
    workload.add_argument("--max-prompt-length", type=int, default=131072, help="Maximum random prompt length in tokens")
    workload.add_argument("--prefix-length", type=int, default=0, help="Shared random prompt prefix length in tokens")
    workload.add_argument("--apply-chat-template", action=argparse.BooleanOptionalAction, default=None, help="Apply the tokenizer chat template")
    workload.add_argument("--max-turns", type=int, default=None, help="Maximum user turns for multi-turn datasets")

    load = parser.add_argument_group("load")
    load.add_argument("--num-requests", "--number", dest="num_requests", type=int, default=100, help="Total request count")
    load.add_argument("--max-concurrency", "--parallel", dest="max_concurrency", type=int, default=1, help="Maximum in-flight requests")
    load.add_argument("--request-rate", "--rate", dest="request_rate", type=float, default=-1.0, help="Poisson request rate in requests/second; -1 disables pacing")
    load.add_argument("--open-loop", action="store_true", help="Do not limit in-flight requests with max concurrency")

    generation = parser.add_argument_group("generation")
    generation.add_argument("--max-tokens", type=int, default=128, help="Maximum generated tokens per request")
    generation.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature")
    generation.add_argument("--stream", action=argparse.BooleanOptionalAction, default=True, help="Use streaming responses")

    output = parser.add_argument_group("results")
    output.add_argument("--name", default="", help="Human-readable run name")
    output.add_argument("--output-dir", "--outputs-dir", dest="output_dir", default="results", help="Local results root")
    output.add_argument("--keep", action="store_true", help="Keep Kubernetes resources after a --deploy run")
    output.add_argument("--wandb", action=argparse.BooleanOptionalAction, default=False, help="Enable Weights & Biases logging")
    output.add_argument("--wandb-project", default="foretoken-bench", help="Weights & Biases project")
    output.add_argument("--wandb-entity", default="", help="Weights & Biases entity")
    output.add_argument("--wandb-run-name", default="", help="Weights & Biases run name")
    output.add_argument("--collect-engine-metrics", action=argparse.BooleanOptionalAction, default=True, help="Collect engine Prometheus metrics")
    output.add_argument("--engine-metrics-url", default="", help="Engine Prometheus /metrics URL")
    output.add_argument("--engine-metrics-interval", type=float, default=1.0, help="Engine metrics polling interval")

    deployment = parser.add_argument_group("Kubernetes deployment")
    deployment.add_argument("--benchmark-image", default=os.environ.get("FORETOKEN_BENCHMARK_IMAGE", ""), help="Benchmark runner image; defaults to FORETOKEN_BENCHMARK_IMAGE")
    deployment.add_argument("--storage-class", default="", help="Results PVC StorageClass; defaults to the cluster default")
    deployment.add_argument("--results-size", default="1Gi", help="Results PVC requested size")
    deployment.add_argument("--service-timeout", type=int, default=900, help="Seconds to wait for Foretoken services")
    deployment.add_argument("--job-timeout", type=int, default=86400, help="Seconds to wait for the Benchmark Job")
    parser.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--execution-context", choices=("endpoint", "managed"), default="endpoint", help=argparse.SUPPRESS)


def _dataset_values(values: list[str]) -> tuple[str, ...]:
    return tuple(item.strip() for value in values for item in value.split(",") if item.strip())


def parse_arguments(argv: Sequence[str] | None = None) -> Command:
    parser = argparse.ArgumentParser(prog="foretoken", description="Foretoken command-line tools")
    commands = parser.add_subparsers(dest="command", required=True)
    bench = commands.add_parser("bench", help="Run inference benchmarks")
    bench_commands = bench.add_subparsers(dest="bench_command", required=True)
    run = bench_commands.add_parser("run", help="Run against an endpoint or a temporary deployment", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _add_run_arguments(run)
    cleanup = bench_commands.add_parser("cleanup", help="Delete Kubernetes resources retained by a managed run")
    cleanup.add_argument("run_id", help="Managed benchmark run ID")
    cleanup.add_argument("--output-dir", default="results", help="Results root containing the run manifest")

    ns = parser.parse_args(argv)
    if ns.bench_command == "cleanup":
        return CleanupCommand(run_id=ns.run_id, output_dir=ns.output_dir)
    datasets = _dataset_values(ns.dataset)
    if ns.base_url and not ns.model:
        run.error("--model is required with --base-url")
    if ns.base_url and ns.keep:
        run.error("--keep is only valid with --deploy")
    if ns.prompt and datasets:
        run.error("--prompt cannot be combined with --dataset")
    return RunCommand(
        base_url=ns.base_url or "", deploy=ns.deploy or "", model=ns.model,
        prompt=ns.prompt or ("" if datasets else "Hello"), datasets=datasets,
        dataset_offset=ns.dataset_offset, tokenizer_path=ns.tokenizer_path,
        min_prompt_length=ns.min_prompt_length, max_prompt_length=ns.max_prompt_length,
        prefix_length=ns.prefix_length, apply_chat_template=ns.apply_chat_template,
        max_turns=ns.max_turns, max_concurrency=ns.max_concurrency,
        num_requests=ns.num_requests, request_rate=ns.request_rate,
        open_loop=ns.open_loop, timeout=ns.timeout, max_retries=ns.max_retries,
        max_tokens=ns.max_tokens, temperature=ns.temperature, stream=ns.stream,
        name=ns.name, output_dir=ns.output_dir, keep=ns.keep,
        benchmark_image=ns.benchmark_image, storage_class=ns.storage_class,
        results_size=ns.results_size, service_timeout=ns.service_timeout,
        job_timeout=ns.job_timeout, wandb=ns.wandb, wandb_project=ns.wandb_project,
        wandb_entity=ns.wandb_entity, wandb_run_name=ns.wandb_run_name,
        collect_engine_metrics=ns.collect_engine_metrics,
        engine_metrics_url=ns.engine_metrics_url,
        engine_metrics_interval=ns.engine_metrics_interval,
        run_id=ns.run_id, execution_context=ns.execution_context,
    )
