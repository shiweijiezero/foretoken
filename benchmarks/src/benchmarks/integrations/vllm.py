# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Normalize vLLM speculative decoding metrics for benchmark observations."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from benchmarks.model_service import ModelService


def speculative_sample_queries(
    service: ModelService, group_names: tuple[str, ...], pod_names: tuple[str, ...],
) -> dict[str, str]:
    """Build native and normalized stage queries for the shared Prometheus sampler."""
    normalized, raw = _spec_selectors(service, group_names, pod_names)
    draft = f"foretoken:model_server_spec_decode_draft_seconds:rate5m{{{normalized}}}"
    target = f"foretoken:model_server_spec_decode_target_forward_seconds:rate5m{{{normalized}}}"
    labels = "namespace,modelservice,model_name,inference_foretoken_io_model_role,engine"

    def token_rate(metric: str) -> str:
        return f"sum by ({labels}) (rate(vllm:spec_decode_num_{metric}_total{{{raw}}}[5m]))"

    proposed = token_rate("draft_tokens")
    accepted = token_rate("accepted_tokens")
    iterations = token_rate("drafts")
    return {
        "spec_draft_gpu_seconds_per_second": draft,
        "spec_target_forward_gpu_seconds_per_second": target,
        "spec_draft_time_share_ratio": f"({draft}) / (({draft}) + ({target}) > 0)",
        "spec_target_forward_time_share_ratio": f"({target}) / (({draft}) + ({target}) > 0)",
        "spec_acceptance_ratio": f"({accepted}) / (({proposed}) > 0)",
        "spec_accepted_tokens_per_draft": f"({accepted}) / (({iterations}) > 0)",
    }


def measure_speculative_window(
    service: ModelService,
    group_names: tuple[str, ...],
    pod_names: tuple[str, ...],
    *,
    duration_seconds: float,
    ended_at: float,
    query: Callable[[str], tuple[dict[str, Any], ...] | None],
) -> dict[str, Any] | None:
    """Read measured-window estimates through the observer's budgeted, error-recording query."""
    if not pod_names:
        return None
    _, raw = _spec_selectors(service, group_names, pod_names)
    window_ms = round(duration_seconds * 1000)
    if window_ms < 1:
        return None
    names = {
        "accepted_tokens": "vllm:spec_decode_num_accepted_tokens_total",
        "draft_tokens": "vllm:spec_decode_num_draft_tokens_total",
        "draft_iterations": "vllm:spec_decode_num_drafts_total",
        "draft_seconds": "vllm:spec_decode_draft_duration_seconds_sum",
        "target_forward_seconds": "vllm:spec_decode_target_forward_duration_seconds_sum",
        "draft_steps": "vllm:spec_decode_draft_duration_seconds_count",
        "target_forward_steps": "vllm:spec_decode_target_forward_duration_seconds_count",
    }
    queries: dict[str, str] = {}
    values: dict[str, float | None] = {}
    for name, metric in names.items():
        # Anchor at measurement completion; publication can take much longer.
        expression = f"sum(increase({metric}{{{raw}}}[{window_ms}ms] @ {ended_at:.3f}))"
        queries[name] = expression
        result = query(expression)
        measured = float(result[0]["value"][1]) if result else None
        values[name] = measured if measured is not None and math.isfinite(measured) else None
    if all(value is None for value in values.values()):
        return None
    accepted, proposed, iterations = (
        values[name] for name in ("accepted_tokens", "draft_tokens", "draft_iterations")
    )
    draft, target = values["draft_seconds"], values["target_forward_seconds"]
    draft_steps, target_steps = values["draft_steps"], values["target_forward_steps"]
    paired_stage_seconds = draft + target if draft is not None and target is not None else None
    return {
        "window_seconds": duration_seconds,
        "window_end_unix_seconds": ended_at,
        "queries": queries,
        "accepted_tokens_estimate": accepted,
        "draft_tokens_estimate": proposed,
        "draft_iterations_estimate": iterations,
        "draft_steps_estimate": draft_steps,
        "target_forward_steps_estimate": target_steps,
        "draft_seconds_estimate": draft,
        "target_forward_seconds_estimate": target,
        "acceptance_ratio": accepted / proposed if accepted is not None and proposed else None,
        "accepted_tokens_per_draft": accepted / iterations if accepted is not None and iterations else None,
        "draft_mean_seconds": draft / draft_steps if draft is not None and draft_steps else None,
        "target_forward_mean_seconds": target / target_steps if target is not None and target_steps else None,
        "draft_time_share_ratio": (
            draft / paired_stage_seconds
            if paired_stage_seconds and draft_steps and target_steps else None
        ),
        "target_forward_time_share_ratio": (
            target / paired_stage_seconds
            if paired_stage_seconds and draft_steps and target_steps else None
        ),
    }


def _spec_selectors(
    service: ModelService, group_names: tuple[str, ...], pod_names: tuple[str, ...],
) -> tuple[str, str]:
    """Scope stage rules and native counters to the service's observed Groups and Pods."""
    namespace = json.dumps(service.model_service_refs[0].namespace)
    model = f",model_name={json.dumps(service.model)}" if service.model else ""
    services = json.dumps("|".join(re.escape(ref.name) for ref in service.model_service_refs))
    groups = json.dumps("|".join(re.escape(name) for name in group_names))
    pods = json.dumps("|".join(re.escape(name) for name in pod_names))
    common = f'namespace={namespace},modelservice=~{services}{model}'
    return (
        f'{common},model_group=~{groups},model_role=~"aggregate|decode"',
        f'{common},pod=~{pods},endpoint="model-server",inference_foretoken_io_model_role=~"aggregate|decode"',
    )
