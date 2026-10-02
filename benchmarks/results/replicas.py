# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Observe ModelService replica status and scheduled GPU requests during a benchmark."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from benchmarks.results.timeseries import ELAPSED_TIME
from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError, ResourceRef

logger = logging.getLogger(__name__)

DEFAULT_REPLICA_SAMPLE_INTERVAL_SECONDS = 1.0
DEFAULT_REPLICA_KUBECTL_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ReplicaTargetObservation:
    """Record applied desired and Ready replicas for one controller scaling target."""

    target_id: str
    role: str
    desired_replicas: int
    ready_replicas: int


@dataclass(frozen=True)
class ReplicaObservation:
    """Record all replica targets returned by one ModelService status read."""

    observed_at: float
    model: str
    model_service: str
    targets: tuple[ReplicaTargetObservation, ...]


@dataclass(frozen=True)
class GPUAllocationSample:
    """Record GPU requests of scheduled Pods belonging to the selected services."""

    observed_at: float
    gpu_counts: dict[str, int]


def _controlled_by(value: dict[str, Any], kind: str, name: str, uid: str) -> bool:
    return any(
        owner.get("kind") == kind
        and owner.get("name") == name
        and owner.get("uid") == uid
        and owner.get("controller") is True
        for owner in value["metadata"].get("ownerReferences", [])
    )


class KubernetesReplicaObserver:
    """Own replica and allocated-GPU sampling for one workload point.

    Initial and final reads bracket the request time axis. A per-run UID map
    retains Group ownership while its terminating Pods outlive their Group.
    Both observations share one bounded kubectl sampling lifecycle.
    """

    def __init__(self, resources: tuple[ResourceRef, ...], model: str) -> None:
        self._resources = resources
        self._model = model
        self._kubectl = Kubectl()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observations: list[ReplicaObservation] = []
        self._errors = 0
        self._last_error: str | None = None
        self._gpu_samples: list[GPUAllocationSample] = []
        self._gpu_failures: list[float] = []
        self._last_gpu_error: str | None = None
        self._service_uids: dict[str, str] | None = None
        self._known_groups: dict[str, str] = {}
        self._known_group_names: set[str] = set()
        self.gpu_allocation: dict[str, Any] | None = None

    def start(self) -> None:
        """Read the pre-measurement boundary, then start one sampling thread."""
        if self._thread is not None:
            raise RuntimeError("replica observer is already active")
        self._sample()
        self._thread = threading.Thread(
            target=self._run,
            name="foretoken-benchmark-resources",
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(DEFAULT_REPLICA_SAMPLE_INTERVAL_SECONDS):
            self._sample()

    def _sample(self) -> None:
        try:
            values = self._kubectl.get_resources(
                self._resources,
                timeout=DEFAULT_REPLICA_KUBECTL_TIMEOUT_SECONDS,
            )
        except (DeploymentError, KeyError, TypeError, ValueError) as exc:
            self._errors += 1
            self._last_error = str(exc)
            self._gpu_failures.append(time.perf_counter())
            self._last_gpu_error = str(exc)
            return
        observed_at = time.perf_counter()
        try:
            self._observations.extend(self._read_observations(values, observed_at))
        except (DeploymentError, KeyError, TypeError, ValueError) as exc:
            self._errors += 1
            self._last_error = str(exc)
        try:
            self._gpu_samples.append(self._read_gpu_sample(values))
        except (DeploymentError, KeyError, TypeError, ValueError) as exc:
            self._gpu_failures.append(time.perf_counter())
            self._last_gpu_error = str(exc)

    def _read_observations(
        self, values: tuple[dict[str, Any], ...], observed_at: float,
    ) -> tuple[ReplicaObservation, ...]:
        by_name = {
            str((value.get("metadata") or {}).get("name") or ""): value
            for value in values
        }
        observations: list[ReplicaObservation] = []
        for resource in self._resources:
            value = by_name.get(resource.name)
            if value is None:
                raise DeploymentError(f"kubectl omitted ModelService/{resource.name}")
            metadata = value.get("metadata") or {}
            status = value.get("status") or {}
            if not isinstance(metadata, dict) or not isinstance(status, dict):
                raise TypeError("ModelService returned invalid metadata or status")
            if status.get("observedGeneration") != metadata.get("generation"):
                continue
            raw_targets = status.get("autoscaling")
            if not isinstance(raw_targets, list) or not raw_targets:
                continue

            targets: list[ReplicaTargetObservation] = []
            for item in raw_targets:
                if not isinstance(item, dict):
                    raise TypeError(
                        "ModelService autoscaling status contains a non-object target"
                    )
                target_id = item.get("id")
                role = item.get("role")
                desired = item.get("appliedReplicas")
                ready = item.get("readyReplicas")
                if not isinstance(target_id, str) or not target_id:
                    raise ValueError("ModelService autoscaling target has no id")
                if not isinstance(role, str) or not role:
                    raise ValueError(
                        f"ModelService autoscaling target {target_id!r} has no role"
                    )
                if (
                    not isinstance(desired, int)
                    or isinstance(desired, bool)
                    or not isinstance(ready, int)
                    or isinstance(ready, bool)
                ):
                    raise TypeError(
                        f"ModelService autoscaling target {target_id!r} has invalid replica counts"
                    )
                targets.append(
                    ReplicaTargetObservation(
                        target_id=target_id,
                        role=role,
                        desired_replicas=desired,
                        ready_replicas=ready,
                    )
                )
            targets.sort(key=lambda item: item.target_id)
            observations.append(
                ReplicaObservation(
                    observed_at=observed_at,
                    model=self._model,
                    model_service=resource.name,
                    targets=tuple(targets),
                )
            )
        return tuple(observations)

    def _read_gpu_sample(self, services: tuple[dict[str, Any], ...]) -> GPUAllocationSample:
        identities = {
            (item["metadata"]["name"], item["metadata"]["uid"])
            for item in services
        }
        if {ref.name for ref in self._resources} != {name for name, _ in identities}:
            raise DeploymentError("kubectl omitted a selected ModelService")
        service_uids = dict(identities)
        if self._service_uids is not None and service_uids != self._service_uids:
            raise DeploymentError("a selected ModelService was replaced during GPU observation")

        output = self._kubectl.run(
            [
                "get", "modelpools,modelgroups,pods", "--namespace",
                self._resources[0].namespace, "-o", "json",
            ],
            timeout=DEFAULT_REPLICA_KUBECTL_TIMEOUT_SECONDS,
        ).stdout
        items = json.loads(output)["items"]
        pools: set[tuple[str, str]] = set()
        live_group_uids = {
            item["metadata"]["uid"] for item in items if item["kind"] == "ModelGroup"
        }
        for item in items:
            if item["kind"] != "ModelPool":
                continue
            ref = item["spec"]["modelServiceRef"]
            if (ref["name"], ref["uid"]) in identities and _controlled_by(
                item, "ModelService", ref["name"], ref["uid"],
            ):
                pools.add((item["metadata"]["name"], item["metadata"]["uid"]))

        # Keep ownership already established in this run: deleting a Group or
        # Pool does not immediately release its terminating Pod's GPU request.
        known_groups = dict(self._known_groups)
        known_group_names = set(self._known_group_names)
        for item in items:
            if item["kind"] != "ModelGroup":
                continue
            ref = item["spec"]["modelPoolRef"]
            if (ref["name"], ref["uid"]) not in pools or not _controlled_by(
                item, "ModelPool", ref["name"], ref["uid"],
            ):
                continue
            known_groups[item["metadata"]["uid"]] = item["spec"]["accelerator"]["deviceResourceName"]
            known_group_names.add(item["metadata"]["name"])

        counts: dict[str, int] = {resource: 0 for resource in set(known_groups.values())}
        for item in items:
            if item["kind"] != "Pod":
                continue
            spec = item["spec"]
            if not spec.get("nodeName") or item["status"]["phase"] in {"Succeeded", "Failed"}:
                continue
            group_name = item["metadata"].get("labels", {}).get(
                "inference.foretoken.io/model-group"
            )
            if group_name is None:
                continue
            model_server = next(
                (container for container in spec["containers"] if container["name"] == "model-server"),
                None,
            )
            if model_server is None:
                raise ValueError(f"Pod {item['metadata']['name']} has no model-server container")
            group_uid = next(
                (env.get("value") for env in model_server.get("env", [])
                 if env.get("name") == "FORETOKEN_MODEL_GROUP_UID"), None,
            )
            if group_uid is None:
                raise ValueError(f"Pod {item['metadata']['name']} lacks its ModelGroup UID")
            device_resource = known_groups.get(group_uid)
            if device_resource is None:
                if group_uid in live_group_uids and group_name not in known_group_names:
                    # Its live Group is not owned by any selected ModelService.
                    continue
                # Without a live Group or previously observed ownership, a
                # terminating Pod cannot safely be attributed to any service.
                raise ValueError(
                    f"Pod {item['metadata']['name']} has unresolvable ModelGroup ownership"
                )
            request = model_server["resources"]["requests"][device_resource]
            count = int(request)
            if count < 1 or str(count) != str(request):
                raise ValueError(
                    f"Pod {item['metadata']['name']} has invalid GPU request {request!r}"
                )
            counts[device_resource] = counts.get(device_resource, 0) + count
        self._service_uids = service_uids
        self._known_groups = known_groups
        self._known_group_names = known_group_names
        return GPUAllocationSample(time.perf_counter(), counts)

    def finish(self, time_origin: float, duration: float | None = None) -> list[dict[str, Any]]:
        """Stop and align replica rows; optionally integrate measured GPU allocation."""
        self.close()
        self._sample()
        if self._errors:
            logger.warning(
                "Replica observation skipped %d Kubernetes reads; last error: %s",
                self._errors, self._last_error,
            )
        if not self._observations:
            logger.warning(
                "No current replica status was available for %s",
                ", ".join(
                    f"ModelService/{resource.name}" for resource in self._resources
                ),
            )
        before: dict[str, ReplicaObservation] = {}
        selected: list[ReplicaObservation] = []
        for item in self._observations:
            if item.observed_at <= time_origin:
                before[item.model_service] = item
            elif duration is None or item.observed_at <= time_origin + duration:
                selected.append(item)
        selected.extend(before.values())
        selected.sort(key=lambda item: (item.observed_at, item.model_service))
        rows: list[dict[str, Any]] = []
        for item in selected:
            rows.append(
                {
                    "elapsed_time_s": max(0.0, item.observed_at - time_origin),
                    "model": item.model,
                    "model_service": item.model_service,
                    "targets": [
                        {
                            "id": target.target_id,
                            "role": target.role,
                            "desired_replicas": target.desired_replicas,
                            "ready_replicas": target.ready_replicas,
                        }
                        for target in item.targets
                    ],
                }
            )
        if duration is not None:
            self.gpu_allocation = self._integrate_gpu(time_origin, duration)
        return rows

    def _integrate_gpu(self, time_origin: float, duration: float) -> dict[str, Any]:
        """Integrate only intervals bracketed by successful Pod snapshots."""
        end = time_origin + duration
        samples = sorted(self._gpu_samples, key=lambda item: item.observed_at)
        failures = sorted(self._gpu_failures)
        area: dict[str, float] = {}
        covered = 0.0
        for previous, following in zip(samples, samples[1:]):
            left = max(time_origin, previous.observed_at)
            right = min(end, following.observed_at)
            if right <= left:
                continue
            # An unsuccessful read leaves the allocation unknown until the
            # next successful snapshot, not zero or the prior sampled count.
            # Reads after the requested window cannot invalidate its area;
            # failures between its boundary snapshots and within the window can.
            if any(
                previous.observed_at < at < following.observed_at and at <= end
                for at in failures
            ):
                continue
            covered += right - left
            for resource, count in previous.gpu_counts.items():
                area[resource] = area.get(resource, 0.0) + count * (right - left)
        complete = duration > 0 and abs(covered - duration) < 1e-6
        if self._gpu_failures:
            logger.warning(
                "GPU allocation observation skipped %d Kubernetes reads; last error: %s",
                len(self._gpu_failures), self._last_gpu_error,
            )
        return {
            "method": "sampled_scheduled_pod_requests_left_hold",
            "duration_s": duration,
            "covered_duration_s": covered,
            "coverage": covered / duration if duration > 0 else None,
            "gpu_seconds": area if complete else None,
            "gpu_hours": (
                {resource: seconds / 3600 for resource, seconds in area.items()}
                if complete else None
            ),
            "observed_gpu_seconds": area,
            "samples": [
                {"elapsed_time_s": sample.observed_at - time_origin, "gpu_counts": sample.gpu_counts}
                for sample in samples
            ],
            "failed_read_elapsed_s": [at - time_origin for at in failures],
            "failed_reads": len(failures),
        }

    def close(self) -> None:
        """Stop and join the sampling thread; repeated calls are harmless."""
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self._stop.set()
        thread.join()


def replica_history_rows(
    observations: list[dict[str, Any]],
) -> list[dict[str, float]]:
    """Map raw replica observations to W&B scalar histories on the shared time axis."""
    rows: list[dict[str, float]] = []
    for observation in observations:
        row: dict[str, float] = {
            ELAPSED_TIME: float(observation["elapsed_time_s"]),
        }
        model_service = str(observation["model_service"])
        for target in observation["targets"]:
            prefix = f"Replicas/{model_service}/{target['id']}"
            row[f"{prefix}/Desired replicas"] = float(target["desired_replicas"])
            row[f"{prefix}/Ready replicas"] = float(target["ready_replicas"])
        rows.append(row)
    return rows


def gpu_allocation_history_rows(
    result: dict[str, Any],
) -> list[dict[str, float | None]]:
    """Project sampled GPU allocations onto the benchmark's elapsed-time axis.

    A pre-origin reading holds at zero only when no read failed before the
    origin. Failures break the held line until the next successful snapshot;
    the right boundary extends the last known count only when a successful
    post-window snapshot brackets it without a failure.
    """
    duration = float(result["duration_s"])
    samples = sorted(result["samples"], key=lambda item: item["elapsed_time_s"])
    failures = sorted(float(at) for at in result["failed_read_elapsed_s"])
    resources = sorted({resource for sample in samples for resource in sample["gpu_counts"]})
    if not resources:
        return []
    fields = {resource: f"Resources/{resource}/Allocated GPUs" for resource in resources}

    def values(sample: dict[str, Any], at: float) -> dict[str, float | None]:
        return {
            ELAPSED_TIME: at,
            **{
                field: float(sample["gpu_counts"][resource])
                for resource, field in fields.items()
                if resource in sample["gpu_counts"]
            },
        }

    events: list[tuple[float, int, dict[str, float | None]]] = []
    before = [sample for sample in samples if sample["elapsed_time_s"] <= 0]
    if before:
        initial = before[-1]
        if not any(initial["elapsed_time_s"] < at <= 0 for at in failures):
            events.append((0.0, 1, values(initial, 0.0)))
    for sample in samples:
        at = float(sample["elapsed_time_s"])
        if 0 < at <= duration:
            events.append((at, 1, values(sample, at)))
    for at in failures:
        if 0 <= at <= duration:
            events.append((at, 0, {ELAPSED_TIME: at, **dict.fromkeys(fields.values(), None)}))

    last = next((sample for sample in reversed(samples) if sample["elapsed_time_s"] <= duration), None)
    bracketed = any(sample["elapsed_time_s"] >= duration for sample in samples)
    if last is not None and bracketed and last["elapsed_time_s"] < duration:
        if not any(last["elapsed_time_s"] < at <= duration for at in failures):
            events.append((duration, 1, values(last, duration)))
    events.sort(key=lambda event: (event[0], event[1]))
    return [row for _, _, row in events]
