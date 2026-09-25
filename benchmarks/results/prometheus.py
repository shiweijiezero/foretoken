# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Sample selected Prometheus series during one Kubernetes benchmark run."""

from __future__ import annotations

import json
import logging
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

    def finish(self, time_origin: float | None) -> dict[str, Any]:
        """Align samples to a perf_counter origin while retaining wall-clock observed_at."""
        self.close()
        prometheus = self._prometheus
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
        }

    def close(self) -> None:
        """Stop and join the sampling thread; repeated calls are harmless."""
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self._stop.set()
        thread.join()
