# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Configure the official AgentX replay while sharing Foretoken service and result choices."""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from benchmarks.config.benchmark import (
    BenchmarkOutputConfig,
    ModelServiceSource,
    ParameterSweepConfig,
    WandbRunConfig,
    parse_duration_seconds,
    select_service,
)
from benchmarks.config.cli import build_service_choices

DEFAULT_AGENTX_DATASET = "semianalysis_cc_traces_weka_062126"


@dataclass
class AgentXConfig:
    """Join the native replay controls with command-owned services, outputs, and repetitions.

    Optional replay values remain absent until the official scenario resolves
    its defaults. Foretoken owns service acquisition and publication.
    """

    services: tuple[ModelServiceSource, ...]
    outputs: BenchmarkOutputConfig = field(default_factory=BenchmarkOutputConfig)
    wandb: WandbRunConfig = field(default_factory=WandbRunConfig)
    sweep: ParameterSweepConfig = field(default_factory=ParameterSweepConfig)
    dataset: str = DEFAULT_AGENTX_DATASET
    input_file: str = ""
    tokenizer_path: str = ""
    tokenizer_trust_remote_code: bool = False
    max_concurrency: int | None = None
    duration: float | None = None
    max_context_length: int | None = None
    random_seed: int | None = None
    warmup_requests_per_lane: int | None = None
    agentic_warmup_grace_period: float | None = None
    benchmark_grace_period: float | None = None
    request_timeout_seconds: float | None = None
    record_processors: int | None = None
    trajectory_start_min_ratio: float | None = None
    trajectory_start_max_ratio: float | None = None
    unsafe_override: bool = False
    extra_body: dict[str, Any] = field(default_factory=dict)

    @property
    def service(self) -> ModelServiceSource:
        """Return the service selected for a single replay or one sweep point."""
        return self.services[0]

    def validate(self, *, require_service: bool = True) -> None:
        """Check Foretoken-owned inputs before service acquisition; native presets own replay policy."""
        if not self.services:
            raise ValueError("provide a Kustomize PATH, --url, or a service in --sweep")
        if require_service:
            for source in self.services:
                source.validate()
        if bool(self.dataset) == bool(self.input_file):
            raise ValueError("select one AgentX --dataset or --input-file")
        if self.max_concurrency is not None and self.max_concurrency < 1:
            raise ValueError("AgentX --max-concurrency must be positive")
        if self.duration is not None and (not math.isfinite(self.duration) or self.duration <= 0):
            raise ValueError("AgentX --duration must be finite and positive")
        if self.sweep.num_runs < 1:
            raise ValueError("--num-runs must be positive")
        self.outputs.validate()
        if self.outputs.includes("raw"):
            raise ValueError("AgentX retains native request metrics; --output raw applies to HTTP benchmarks")
        if self.outputs.includes("wandb") and not self.wandb.project.strip():
            raise ValueError("--wandb-project must not be empty")

    def to_dict(self) -> dict[str, Any]:
        """Snapshot replay choices for results and resume without storing service credentials."""
        services = [source.to_dict() for source in self.services]
        for source in services:
            source.pop("timeout")
            source.pop("max_retries")
        return {
            "mode": "agentx",
            "service": services[0],
            "service_choices": services,
            "dataset": self.dataset,
            "input_file": self.input_file,
            "tokenizer_path": self.tokenizer_path,
            "tokenizer_trust_remote_code": self.tokenizer_trust_remote_code,
            "load": {
                "max_concurrency": self.max_concurrency, "duration": self.duration,
                "max_context_length": self.max_context_length, "random_seed": self.random_seed,
                "warmup_requests_per_lane": self.warmup_requests_per_lane,
                "agentic_warmup_grace_period": self.agentic_warmup_grace_period,
                "benchmark_grace_period": self.benchmark_grace_period,
                "request_timeout_seconds": self.request_timeout_seconds,
                "record_processors": self.record_processors,
                "trajectory_start_min_ratio": self.trajectory_start_min_ratio,
                "trajectory_start_max_ratio": self.trajectory_start_max_ratio,
            },
            "unsafe_override": self.unsafe_override,
            "extra_body": self.extra_body,
            "output": {
                "destinations": list(self.outputs.destinations),
                "output_dir": self.outputs.output_dir, "iteration": self.outputs.iteration,
            },
            "wandb": {
                "project": self.wandb.project, "entity": self.wandb.entity,
                "group": self.wandb.group, "run_name": self.wandb.run_name,
            },
            "sweep": {
                "path": self.sweep.path, "num_runs": self.sweep.num_runs,
                "experiment_name": self.sweep.experiment_name,
            },
        }


def _json_object(value: str) -> dict[str, Any]:
    """Decode request-body additions without accepting non-object native inputs."""
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("--extra-body must be a JSON object")
    return parsed


def parse_agentx_arguments(argv: Sequence[str]) -> AgentXConfig:
    """Parse the AgentX performance mode without importing or installing its native runner."""
    source, output, tracking, sweep = ModelServiceSource(), BenchmarkOutputConfig(), WandbRunConfig(), ParameterSweepConfig()
    parser = argparse.ArgumentParser(
        prog="foretoken perf --agentx", allow_abbrev=False,
        description="Replay AgentX sessions with the official harness against a model service.",
    )
    parser.add_argument("--agentx", action="store_true", required=True, help=argparse.SUPPRESS)
    parser.add_argument("paths", nargs="*", metavar="PATH", help="Kustomize deployments to reuse or measure sequentially")
    parser.add_argument("--url", nargs="+", default=[], help="Existing Chat Completions endpoints")
    parser.add_argument("--model", nargs="+", default=[], help="One shared model ID or one per service")
    parser.add_argument("--name", dest="instance_name", default=source.instance_name, help="Instance name for one PATH")
    parser.add_argument("--api-key", default=source.api_key)
    parser.add_argument("--health-url", default=source.health_url)
    parser.add_argument("--wait-timeout", default=source.wait_timeout)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--dataset", default=DEFAULT_AGENTX_DATASET, help="Official public AgentX dataset selector")
    inputs.add_argument("--input-file", default="", help="Local Weka trace file consumed by the official replay")
    parser.add_argument("--tokenizer-path", default="", help="Tokenizer ID or local path; inferred from the service")
    parser.add_argument("--tokenizer-trust-remote-code", action="store_true")
    # The official scenario supplies defaults for these optional replay controls.
    for flag, decoder, help_text in (
        ("--max-concurrency", int, "Concurrent agent sessions"),
        ("--duration", parse_duration_seconds, "Profiling duration, for example 1h or 30s"),
        ("--max-context-length", int, "Maximum replay context length"),
        ("--random-seed", int, "Replay sampling and reconstruction seed"),
        ("--warmup-requests-per-lane", int, "Warmup requests for each replay lane"),
        ("--agentic-warmup-grace-period", parse_duration_seconds, "Grace period for agentic warmup"),
        ("--benchmark-grace-period", parse_duration_seconds, "Grace period after profiling"),
        ("--request-timeout-seconds", parse_duration_seconds, "Native per-request timeout"),
        ("--record-processors", int, "Native client processes for metric records"),
        ("--trajectory-start-min-ratio", float, "Minimum relative trajectory starting point"),
        ("--trajectory-start-max-ratio", float, "Maximum relative trajectory starting point"),
    ):
        parser.add_argument(flag, type=decoder, default=None, help=help_text)
    parser.add_argument("--unsafe-override", action="store_true", help="Allow native scenario overrides for exploratory runs")
    parser.add_argument("--extra-body", type=_json_object, default={}, help="JSON request-body additions")
    parser.add_argument("--output", type=lambda value: tuple(value.split(",")), default=output.destinations)
    parser.add_argument("--output-dir", default=output.output_dir)
    parser.add_argument("--iteration", default=output.iteration)
    parser.add_argument("--wandb-project", default=tracking.project)
    parser.add_argument("--wandb-entity", default=tracking.entity)
    parser.add_argument("--wandb-run-name", default=tracking.run_name)
    parser.add_argument("--wandb-group", default=tracking.group)
    parser.add_argument("--sweep", default=sweep.path, help="JSONL replay configurations")
    parser.add_argument("--num-runs", type=int, default=sweep.num_runs)
    parser.add_argument("--experiment-name", default=sweep.experiment_name)
    parser.add_argument("--resume", default=sweep.resume, help="Recover saved outputs or continue incomplete sweep points")
    args = parser.parse_args(list(argv))
    paths, urls, models = tuple(args.paths), tuple(args.url), tuple(args.model)
    if paths and urls:
        parser.error("provide Kustomize paths or --url values, not both")
    if args.instance_name and len(paths) != 1:
        parser.error("--name requires exactly one Kustomize PATH")
    if models and len(models) not in (1, len(paths or urls)):
        parser.error("--model accepts one shared value or one value per service")
    choices = build_service_choices(paths, urls, models)
    base = replace(source, model=models[0] if len(models) == 1 else "", api_key=args.api_key, health_url=args.health_url,
                   wait_timeout=args.wait_timeout, instance_name=args.instance_name)
    services = tuple(select_service(base, {"health_url": args.health_url, **choice}) for choice in choices) or (base,)
    config = AgentXConfig(
        services=services,
        outputs=BenchmarkOutputConfig(args.output, args.output_dir, args.iteration),
        wandb=WandbRunConfig(args.wandb_project, args.wandb_entity, args.wandb_group, args.wandb_run_name),
        sweep=ParameterSweepConfig(args.sweep, args.num_runs, args.experiment_name, args.resume),
        dataset="" if args.input_file else args.dataset, input_file=args.input_file,
        tokenizer_path=args.tokenizer_path, tokenizer_trust_remote_code=args.tokenizer_trust_remote_code,
        max_concurrency=args.max_concurrency, duration=args.duration, max_context_length=args.max_context_length,
        random_seed=args.random_seed, warmup_requests_per_lane=args.warmup_requests_per_lane,
        agentic_warmup_grace_period=args.agentic_warmup_grace_period,
        benchmark_grace_period=args.benchmark_grace_period, request_timeout_seconds=args.request_timeout_seconds,
        record_processors=args.record_processors,
        trajectory_start_min_ratio=args.trajectory_start_min_ratio, trajectory_start_max_ratio=args.trajectory_start_max_ratio,
        unsafe_override=args.unsafe_override, extra_body=args.extra_body,
    )
    config.validate(require_service=not bool(config.sweep.path))
    return config
