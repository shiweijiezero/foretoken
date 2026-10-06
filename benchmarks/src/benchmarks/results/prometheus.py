# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Sample selected Prometheus series during one Kubernetes benchmark run."""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError
from foretoken.observability import PrometheusRef, prometheus_query, select_prometheus
from foretoken.platform.config import default_platform_config

if TYPE_CHECKING:
    from benchmarks.model_service import ModelService

logger = logging.getLogger(__name__)

_SAMPLE_INTERVAL_SECONDS = 5.0
_REQUEST_TIMEOUT = "5s"


class PrometheusObserver:
    """Own bounded Prometheus sampling for one Kubernetes model benchmark."""

    def __init__(self, service: ModelService) -> None:
        self._service = service
        self._kubectl: Kubectl | None = None
        self._prometheus: PrometheusRef | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[tuple[float, dict[str, Any]]] = []
        self._errors = 0
        self._last_error: str | None = None

    def start(self) -> None:
        """Select the installed Prometheus and start periodic sampling."""
        if not self._service.model_service_refs:
            raise DeploymentError("Prometheus observation requires a Kustomize service")
        if self._thread is not None:
            raise RuntimeError("Prometheus observer is already active")
        kubectl = Kubectl()
        prometheus = select_prometheus(
            kubectl,
            default_platform_config().namespace,
            None,
        )
        if prometheus is None:
            raise DeploymentError("no compatible Prometheus is available")
        self._kubectl = kubectl
        self._prometheus = prometheus
        self._thread = threading.Thread(
            target=self._run,
            name="foretoken-benchmark-prometheus",
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sampled_at = time.perf_counter()
                sample = self._read_sample()
            except (DeploymentError, KeyError, TypeError, ValueError) as exc:
                self._errors += 1
                self._last_error = str(exc)
            else:
                self._samples.append((sampled_at, sample))
            self._stop.wait(_SAMPLE_INTERVAL_SECONDS)

    def _spec_selectors(self) -> tuple[str, str]:
        """Scope stage rules and raw counters to this model's selected ModelServices."""
        namespace = json.dumps(self._service.model_service_refs[0].namespace)
        model = json.dumps(self._service.model)
        services = json.dumps("|".join(re.escape(ref.name) for ref in self._service.model_service_refs))
        common = f'namespace={namespace},model_name={model},modelservice=~{services}'
        return (
            f'{common},model_role=~"aggregate|decode"',
            f'{common},endpoint="model-server",inference_foretoken_io_model_role=~"aggregate|decode"',
        )

    def _read_sample(self) -> dict[str, Any]:
        if self._kubectl is None or self._prometheus is None:
            raise RuntimeError("Prometheus observer is not active")
        namespace = self._service.model_service_refs[0].namespace
        namespace_label = json.dumps(namespace)
        model_filter = f",model_name={json.dumps(self._service.model)}" if self._service.model else ""
        expressions = {
            "requests_running": (
                f"foretoken:model_server_requests_running:sum{{namespace={namespace_label}}}"
            ),
            "requests_waiting": (
                f"foretoken:model_server_requests_waiting:sum{{namespace={namespace_label}}}"
            ),
            "prompt_tokens_per_second": (
                f"foretoken:model_server_prompt_tokens:rate5m{{namespace={namespace_label}}}"
            ),
            "generation_tokens_per_second": (
                f"foretoken:model_server_generation_tokens:rate5m{{namespace={namespace_label}}}"
            ),
            "kv_cache_usage_ratio": (
                f"foretoken:model_server_kv_cache_usage_ratio:max{{namespace={namespace_label}}}"
            ),
            "prefix_cache_hit_ratio": (
                f"foretoken:model_server_prefix_cache_hit_ratio:rate5m{{namespace={namespace_label}}}"
            ),
            "gpu_utilization_ratio": (
                f"foretoken:accelerator_gpu_utilization_ratio{{namespace={namespace_label}}}"
            ),
            "gpu_memory_usage_ratio": (
                f"foretoken:accelerator_gpu_memory_usage_ratio{{namespace={namespace_label}}}"
            ),
            "gpu_power_watts": (
                f"foretoken:accelerator_gpu_power_watts{{namespace={namespace_label}}}"
            ),
            "gpu_temperature_celsius": (
                f"foretoken:accelerator_gpu_temperature_celsius{{namespace={namespace_label}}}"
            ),
            "routing_share_rate": (
                "sum by(model_name,model_role,route_target_id,data_parallel_rank) "
                f"(rate(foretoken_router_target_selections_total{{namespace={namespace_label}"
                f"{model_filter}}}[1m]))"
            ),
        }
        normalized, raw = self._spec_selectors()
        draft = f"foretoken:model_server_spec_decode_draft_seconds:rate5m{{{normalized}}}"
        target = f"foretoken:model_server_spec_decode_target_forward_seconds:rate5m{{{normalized}}}"
        labels = "namespace,modelservice,model_name,inference_foretoken_io_model_role,engine"
        def token_rate(metric: str) -> str:
            return f"sum by ({labels}) (rate(vllm:spec_decode_num_{metric}_total{{{raw}}}[5m]))"
        proposed = token_rate("draft_tokens")
        accepted = token_rate("accepted_tokens")
        iterations = token_rate("drafts")
        expressions.update({
            "spec_draft_gpu_seconds_per_second": draft,
            "spec_target_forward_gpu_seconds_per_second": target,
            "spec_draft_time_share_ratio": f"({draft}) / (({draft}) + ({target}) > 0)",
            "spec_target_forward_time_share_ratio": f"({target}) / (({draft}) + ({target}) > 0)",
            "spec_acceptance_ratio": f"({accepted}) / (({proposed}) > 0)",
            "spec_accepted_tokens_per_draft": f"({accepted}) / (({iterations}) > 0)",
        })
        return {
            "observed_at": time.time(),
            "queries": {
                name: {
                    "expression": expression,
                    "result": list(
                        prometheus_query(
                            self._kubectl,
                            self._prometheus,
                            expression,
                            _REQUEST_TIMEOUT,
                        )
                    ),
                }
                for name, expression in expressions.items()
            },
        }

    def _measure_spec_window(self, duration_seconds: float, ended_at: float) -> dict[str, Any] | None:
        """Estimate measured-window counters at a fixed end time, including counter resets."""
        if self._kubectl is None or self._prometheus is None:
            return None
        _, raw = self._spec_selectors()
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
            result = prometheus_query(self._kubectl, self._prometheus, expression, _REQUEST_TIMEOUT)
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

    def finish(self, time_origin: float | None, duration_seconds: float | None = None) -> dict[str, Any]:
        """Align samples and estimate speculative metrics over the measured request window."""
        ended_at = (
            time.time() - (time.perf_counter() - time_origin - duration_seconds)
            if time_origin is not None and duration_seconds is not None else None
        )
        self.close()
        prometheus = self._prometheus
        summary = None
        if ended_at is not None and duration_seconds is not None:
            try:
                summary = self._measure_spec_window(duration_seconds, ended_at)
            except (DeploymentError, KeyError, TypeError, ValueError) as exc:
                self._errors += 1
                self._last_error = str(exc)
        rows = []
        for sampled_at, sample in self._samples:
            row = dict(sample)
            if time_origin is not None:
                row["elapsed_time_s"] = max(0.0, sampled_at - time_origin)
            rows.append(row)
        return {
            "source": (
                {
                    "namespace": prometheus.namespace,
                    "name": prometheus.name,
                }
                if prometheus is not None
                else None
            ),
            "sample_interval_s": _SAMPLE_INTERVAL_SECONDS,
            "errors": self._errors,
            "last_error": self._last_error,
            "samples": rows,
            "speculative_decoding": summary,
        }

    def close(self) -> None:
        """Stop and join the sampling thread; repeated calls are harmless."""
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self._stop.set()
        thread.join()
