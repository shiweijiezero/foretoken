# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Sample selected Prometheus series during one Kubernetes benchmark run."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl
from foretoken.platform.config import default_platform_config
from foretoken.platform.helm import Helm
from foretoken.platform.prometheus import (
    PrometheusRef,
    prometheus_api_path,
    prometheus_query,
    select_prometheus,
)

from benchmarks.integrations.vllm import (
    measure_speculative_window,
    speculative_sample_queries,
)
from benchmarks.results.observations.environment import OBSERVATION_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from benchmarks.model_service import ModelService
    from benchmarks.results.observations.replicas import KubernetesReplicaObserver

_SAMPLE_INTERVAL_SECONDS = 5.0
_REQUEST_TIMEOUT = f"{OBSERVATION_TIMEOUT_SECONDS:g}s"


class PrometheusObserver:
    """Own bounded Prometheus sampling for one Kubernetes model benchmark."""

    def __init__(self, service: ModelService, resources: KubernetesReplicaObserver) -> None:
        self._service = service
        self._resources = resources
        self._prometheus: PrometheusRef | None = None
        self._api_path: str | None = None
        self._stop = threading.Event()
        self._stop_deadline: float | None = None
        self._thread: threading.Thread | None = None
        self._samples: list[tuple[float, dict[str, Any]]] = []
        self._errors = 0
        self._last_error: str | None = None

    def start(self, *, deadline: float | None = None) -> None:
        """Resolve Prometheus and its HTTP Service once, then sample within bounded rounds."""
        if not self._service.model_service_refs:
            raise DeploymentError("Prometheus observation requires a Kustomize service")
        if self._thread is not None:
            raise RuntimeError("Prometheus observer is already active")
        if deadline is None:
            deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        kubectl = self._service.kubectl
        platform = default_platform_config()
        with kubectl.until(deadline):
            configuration = Helm(platform, kubectl).platform_configuration()
            values = json.loads(configuration["data"]["deploymentValues"])
            requested = values["observability"]["prometheus"] or None
            prometheus = select_prometheus(kubectl, platform.namespace, requested)
            if prometheus is None:
                raise DeploymentError("no compatible Prometheus is available")
            self._api_path = prometheus_api_path(kubectl, prometheus)
        self._prometheus = prometheus
        self._thread = threading.Thread(
            target=self._run,
            name="foretoken-benchmark-prometheus",
        )
        self._thread.start()

    def finish(
        self, time_origin: float | None, duration_seconds: float | None = None, *, deadline: float | None = None,
    ) -> dict[str, Any]:
        """Align samples and estimate window counters within the result owner's remaining budget."""
        ended_at = (
            time.time() - (time.perf_counter() - time_origin - duration_seconds)
            if time_origin is not None and duration_seconds is not None else None
        )
        if deadline is None:
            deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        self.close(deadline=deadline)
        kubectl = self._service.kubectl
        prometheus = self._prometheus
        summary = None
        resources = None
        if ended_at is not None and duration_seconds is not None and self._prometheus is not None:
            try:
                with kubectl.until(deadline):
                    summary = measure_speculative_window(
                        self._service, self._resources.group_names, self._resources.pod_names,
                        duration_seconds=duration_seconds, ended_at=ended_at,
                        query=lambda expression: self._query(kubectl, expression),
                    )
            except (DeploymentError, KeyError, TypeError, ValueError) as exc:
                self._errors += 1
                self._last_error = str(exc)
        if ended_at is not None and duration_seconds is not None:
            try:
                with kubectl.until(deadline):
                    resources = self._measure_resource_window(time_origin, duration_seconds, ended_at, kubectl)
            except DeploymentError as exc:
                self._errors += 1
                self._last_error = str(exc)
        rows = []
        samples = self._samples
        if time_origin is not None:
            before = [item for item in samples if item[0] <= time_origin]
            within = [
                item for item in samples
                if item[0] > time_origin
                and (duration_seconds is None or item[0] <= time_origin + duration_seconds)
            ]
            samples = before[-1:] + within
        for sampled_at, sample in samples:
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
            "resources": resources,
        }

    def stop(self, deadline: float) -> None:
        """Stop scheduling queries before the result owner joins its observation threads."""
        self._stop_deadline = deadline if self._stop_deadline is None else min(deadline, self._stop_deadline)
        self._stop.set()

    def close(self, *, deadline: float | None = None) -> None:
        """Join the bounded in-flight query; repeated calls are harmless."""
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self.stop(deadline if deadline is not None else time.monotonic() + OBSERVATION_TIMEOUT_SECONDS)
        thread.join()

    def _run(self) -> None:
        """Sample until stopped, retaining query failures separately from missing metric values."""
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

    def _query(self, kubectl: Kubectl, expression: str) -> tuple[dict[str, Any], ...] | None:
        """Keep one unavailable query distinct from an empty series without discarding other readings."""
        try:
            return prometheus_query(
                kubectl, self._prometheus, expression, _REQUEST_TIMEOUT, api_path=self._api_path,
            )
        except DeploymentError as error:
            self._errors += 1
            self._last_error = str(error)
            return None

    def _read_sample(self) -> dict[str, Any]:
        """Read one bounded set of serving-scoped queries, retaining expressions, labels, and device identities."""
        if self._prometheus is None:
            raise RuntimeError("Prometheus observer is not active")
        deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        if self._stop_deadline is not None:
            deadline = min(deadline, self._stop_deadline)
        kubectl = self._service.kubectl
        namespace = self._service.model_service_refs[0].namespace
        namespace_label = json.dumps(namespace)
        model_filter = f",model_name={json.dumps(self._service.model)}" if self._service.model else ""
        groups = "|".join(re.escape(name) for name in self._resources.group_names)
        if not groups or not self._resources.pod_names:
            return {"observed_at": time.time(), "queries": {}, "gpu_devices": None}
        group_scope = f'namespace={namespace_label},model_group=~{json.dumps(groups)}'
        targets = json.dumps("|".join(re.escape(uid) for uid in self._resources.group_uids))
        expressions = {
            "requests_running": (
                f"foretoken:model_server_requests_running:sum{{{group_scope}}}"
            ),
            "requests_waiting": (
                f"foretoken:model_server_requests_waiting:sum{{{group_scope}}}"
            ),
            "prompt_tokens_per_second": (
                f"foretoken:model_server_prompt_tokens:rate5m{{{group_scope}}}"
            ),
            "generation_tokens_per_second": (
                f"foretoken:model_server_generation_tokens:rate5m{{{group_scope}}}"
            ),
            "kv_cache_usage_ratio": (
                f"foretoken:model_server_kv_cache_usage_ratio:max{{{group_scope}}}"
            ),
            "prefix_cache_hit_ratio": (
                f"foretoken:model_server_prefix_cache_hit_ratio:rate5m{{{group_scope}}}"
            ),
            "gpu_utilization_ratio": (
                f"foretoken:accelerator_gpu_utilization_ratio{{{group_scope}}}"
            ),
            "gpu_memory_usage_ratio": (
                f"foretoken:accelerator_gpu_memory_usage_ratio{{{group_scope}}}"
            ),
            "gpu_power_watts": (
                f"foretoken:accelerator_gpu_power_watts{{{group_scope}}}"
            ),
            "gpu_temperature_celsius": (
                f"foretoken:accelerator_gpu_temperature_celsius{{{group_scope}}}"
            ),
            "routing_share_rate": (
                "sum by(model_name,model_role,route_target_id,data_parallel_rank) "
                f"(rate(foretoken_router_target_selections_total{{namespace={namespace_label}"
                f",route_target_id=~{targets}{model_filter}}}[1m]))"
            ),
        }
        containers = self._container_selector()
        if containers is not None:
            labels = "namespace,pod,container,id"
            quota = f"max by ({labels}) (container_spec_cpu_quota{{{containers}}})"
            period = f"max by ({labels}) (container_spec_cpu_period{{{containers}}})"
            periods = f"max by ({labels}) (rate(container_cpu_cfs_periods_total{{{containers}}}[1m]))"
            throttled = f"max by ({labels}) (rate(container_cpu_cfs_throttled_periods_total{{{containers}}}[1m]))"
            expressions.update({
                "cpu_usage_cores": f"max by ({labels}) (rate(container_cpu_usage_seconds_total{{{containers}}}[1m]))",
                "cpu_quota_cores": f"({quota} > 0) / ({period} > 0)",
                "cpu_quota_microseconds": quota,
                "cpu_throttled_periods_ratio": f"({throttled}) / ({periods} > 0)",
                "memory_working_set_bytes": f"max by ({labels}) (container_memory_working_set_bytes{{{containers}}})",
            })
        expressions.update(speculative_sample_queries(
            self._service, self._resources.group_names, self._resources.pod_names,
        ))
        observed_at = time.time()
        queries = {}
        with kubectl.until(deadline):
            for name, expression in expressions.items():
                if self._stop.is_set():
                    break
                result = self._query(kubectl, expression)
                queries[name] = {"expression": expression, "result": list(result or ())}
                if result is None:
                    queries[name]["error"] = self._last_error
                    break
        # Zero utilization still identifies an allocated device. Preserve the exporter
        # identity rather than interpreting missing samples as zero allocated GPUs.
        device_keys = ("namespace", "pod", "container", "node", "model_group", "vendor", "device_id")
        devices = {
            tuple(result["metric"].get(key) for key in device_keys)
            for name, query in queries.items() if name.startswith("gpu_")
            for result in query["result"] if result.get("metric", {}).get("device_id")
        }
        return {
            "observed_at": observed_at, "queries": queries,
            "gpu_devices": [dict(zip(device_keys, device)) for device in sorted(devices, key=str)] or None,
        }

    def _container_selector(self) -> str | None:
        """Select only serving Pods whose ownership the resource observer verified."""
        pods = "|".join(re.escape(name) for name in self._resources.pod_names)
        if not pods:
            return None
        namespace = json.dumps(self._service.model_service_refs[0].namespace)
        return f'namespace={namespace},pod=~{json.dumps(pods)},container="model-server"'

    def _measure_resource_window(
        self, time_origin: float, duration_seconds: float, ended_at: float, kubectl: Kubectl,
    ) -> dict[str, Any] | None:
        """Estimate container CPU counters within the formal measurement window, including retired Pods."""
        containers = self._container_selector()
        window_ms = round(duration_seconds * 1000)
        if containers is None or window_ms < 1:
            return None
        names = {
            "cpu_usage_seconds": "container_cpu_usage_seconds_total",
            "cpu_periods": "container_cpu_cfs_periods_total",
            "cpu_throttled_periods": "container_cpu_cfs_throttled_periods_total",
            "cpu_throttled_seconds": "container_cpu_cfs_throttled_seconds_total",
        }
        expected = {
            (pod["name"], pod["container_id"].split("://", 1)[-1])
            for sample in self._resources.workload_history(time_origin, duration_seconds)
            for pod in sample["pods"] if pod.get("container_id")
        }
        allocation = self._resources.gpu_allocation
        ownership_complete = allocation is not None and allocation["gpu_seconds"] is not None
        values = {}
        queries = {}
        coverage = {}
        for name, metric in names.items():
            expression = (
                "max by (namespace,pod,container,id) ("
                f"increase({metric}{{{containers}}}[{window_ms}ms] @ {ended_at:.3f}))"
            )
            result = self._query(kubectl, expression)
            queries[name] = {"expression": expression, "result": list(result or ())}
            if result is None:
                queries[name]["error"] = self._last_error
            observed = {}
            unmatched = False
            for item in result or ():
                labels = item["metric"]
                value = float(item["value"][1])
                identity = next((key for key in expected
                                 if labels.get("pod") == key[0] and key[1] in labels.get("id", "")), None)
                if identity is None or not math.isfinite(value):
                    unmatched = True
                else:
                    observed[identity] = value
            complete = bool(expected) and ownership_complete and not unmatched and observed.keys() == expected
            coverage[name] = {"observed_containers": len(observed), "expected_containers": len(expected), "complete": complete}
            values[name] = sum(observed.values()) if complete else None
        usage, periods, throttled = values["cpu_usage_seconds"], values["cpu_periods"], values["cpu_throttled_periods"]
        return {
            "window_seconds": duration_seconds, "window_end_unix_seconds": ended_at,
            "queries": queries, "coverage": coverage, **values,
            "cpu_usage_cores": usage / duration_seconds if usage is not None else None,
            "cpu_throttled_periods_ratio": throttled / periods if throttled is not None and periods else None,
        }
