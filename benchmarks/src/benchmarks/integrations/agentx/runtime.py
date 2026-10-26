# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run the released AgentX CLI in uv's cached, isolated Python environment."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from benchmarks.integrations.agentx import HARNESS_VERSION

if TYPE_CHECKING:
    from benchmarks.config.agentx import AgentXConfig
    from benchmarks.model_service import ModelService


def profile_command(
    config: AgentXConfig, service: ModelService, native_directory: Path,
) -> tuple[list[str], dict[str, str]]:
    """Build the native CLI invocation and a child-only authentication environment.

    uv owns dependency resolution, interpreter selection, and environment reuse.
    AgentX owns its scenario defaults; only explicit workload choices override them.
    """
    endpoint = urlsplit(service.chat_completions_url)
    arguments = [
        sys.executable, "-m", "uv", "--quiet", "tool", "run", "--python", "3.13",
        "--from", f"agentx-harness=={HARNESS_VERSION}", "agentx", "profile",
        "--url", urlunsplit((endpoint.scheme, endpoint.netloc, "", "", "")),
        "--endpoint", urlunsplit(("", "", endpoint.path, endpoint.query, "")),
        "--model", service.model,
        "--tokenizer", config.tokenizer_path or service.tokenizer_identity[1],
        "--output-artifact-dir", str(native_directory),
        "--export-level", "records", "--ui", "none", "--no-auto-plot",
        "--use-legacy-max-tokens",
    ]
    if config.input_file:
        arguments += ["--input-file", str(Path(config.input_file).resolve()), "--custom-dataset-type", "weka_trace"]
    else:
        arguments += ["--public-dataset", config.dataset]
    for flag, value in (
        ("--concurrency", config.max_concurrency),
        ("--benchmark-duration", config.duration),
        ("--max-context-length", config.max_context_length),
        ("--random-seed", config.random_seed),
        ("--warmup-requests-per-lane", config.warmup_requests_per_lane),
        ("--agentic-warmup-grace-period", config.agentic_warmup_grace_period),
        ("--benchmark-grace-period", config.benchmark_grace_period),
        ("--request-timeout-seconds", config.request_timeout_seconds),
        ("--record-processors", config.record_processors),
        ("--trajectory-start-min-ratio", config.trajectory_start_min_ratio),
        ("--trajectory-start-max-ratio", config.trajectory_start_max_ratio),
    ):
        if value is not None:
            arguments += [flag, str(value)]
    if config.tokenizer_trust_remote_code:
        arguments.append("--tokenizer-trust-remote-code")
    if config.unsafe_override:
        arguments.append("--unsafe-override")
    # Timing clients must observe reasoning tokens emitted by a Foretoken frontend.
    extra_body = {"include_reasoning": True, **config.extra_body} if service.deployment is not None else config.extra_body
    if extra_body:
        arguments += ["--extra-inputs", json.dumps(extra_body, ensure_ascii=False)]
    for name, value in service.request_headers.items():
        arguments += ["--header", f"{name}: {value}"]
    environment = os.environ.copy()
    if hub_endpoint := environment.get("FORETOKEN_HF_ENDPOINT"):
        environment["HF_ENDPOINT"] = hub_endpoint
    if service.api_key and service.api_key != "EMPTY":
        environment["FORETOKEN_AGENTX_API_KEY"] = service.api_key
        arguments += ["--api-key", "${FORETOKEN_AGENTX_API_KEY}"]
    return arguments, environment
