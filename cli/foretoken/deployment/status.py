# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Observe controller-owned service readiness for deployment and status commands."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from foretoken.arguments import StatusCommand
from foretoken.deployment.manifest import load_deployment, service_requires_execution
from foretoken.deployment.progress import StartupProgress
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, ResourceRef, resource_ref, timeout_seconds


@dataclass(frozen=True)
class ResourceProgress:
    """Current service state and convergence of its requested running or stopped intent."""

    resource: ResourceRef
    state: str
    reason: str
    message: str
    converged: bool

    @property
    def detail(self) -> str:
        """Return the most useful concise status explanation."""
        return self.message or self.reason


def resource_progress(
    resource: ResourceRef, value: dict[str, Any], *, expected_revision: str | None = None
) -> ResourceProgress:
    """Interpret current serving and explicitly selected alert readiness without accepting stale generations."""
    metadata = value.get("metadata") or {}
    status = value.get("status") or {}
    if metadata.get("deletionTimestamp"):
        return ResourceProgress(
            resource, "Terminating", "Deleting", "Resource is being deleted", False
        )

    if expected_revision is not None and (value.get("spec") or {}).get("deploymentRevision") != expected_revision:
        return ResourceProgress(
            resource, "Failed", "DeploymentSuperseded",
            "A newer deployment replaced this operation's target", False,
        )

    generation = int(metadata.get("generation") or 0)
    observed_generation = int(status.get("observedGeneration") or 0)
    conditions = status.get("conditions") or []
    execution_required = service_requires_execution(value)
    suspended = (value.get("spec") or {}).get("suspend", False)
    alerts = ((value.get("spec") or {}).get("observability") or {}).get("alerts") or {}
    alerts_selected = bool(alerts.get("rules"))
    alerts_condition = next(
        (item for item in conditions if item.get("type") == "AlertsReady"), None
    )
    alerts_current = (
        alerts_condition is not None
        and int(alerts_condition.get("observedGeneration") or 0) == generation
    )
    if not suspended and alerts_current and alerts_condition.get("status") == "False":
        return ResourceProgress(
            resource, "Failed", str(alerts_condition.get("reason") or "AlertsFailed"),
            str(alerts_condition.get("message") or "Service alerts are not configured"), False,
        )
    ready_condition = next(
        (
            condition
            for condition in conditions
            if isinstance(condition, dict) and condition.get("type") == "Ready"
        ),
        None,
    )
    if ready_condition is None:
        return ResourceProgress(
            resource,
            "Progressing",
            "Reconciling",
            "Waiting for controller status",
            False,
        )

    reason = str(ready_condition.get("reason") or "Reconciling")
    message = str(ready_condition.get("message") or "")
    condition_generation = int(ready_condition.get("observedGeneration") or 0)
    if observed_generation != generation or condition_generation != generation:
        return ResourceProgress(
            resource,
            "Progressing",
            "Updating",
            "Waiting for the current configuration to be reconciled",
            False,
        )

    condition_status = str(ready_condition.get("status") or "Unknown")
    target = next((item for item in conditions if item.get("type") == "TargetReady"), None)
    if target is not None and int(target.get("observedGeneration") or 0) == generation and target.get("status") == "False":
        message = str(target["message"])
        if resource.kind == "ModelService" and condition_status == "True" and int(status.get("servingGeneration") or 0) != generation:
            message += "\nThe previous generation remains available."
        return ResourceProgress(resource, "Failed", str(target["reason"]), message, False)
    if (
        condition_status == "True"
        and target is not None
        and int(target.get("observedGeneration") or 0) == generation
        and target.get("status") != "True"
    ):
        return ResourceProgress(
            resource, "Progressing", str(target["reason"]), str(target["message"]), False
        )
    stopped = (
        condition_status == "False"
        and reason == "ScaledToZero"
        and not execution_required
        and (
            resource.kind == "FrontendService"
            or resource.kind == "ModelService"
            and target is not None
            and target.get("observedGeneration") == generation
            and target.get("status") == "True"
            and target.get("reason") == "ScaledToZero"
        )
    )
    if condition_status == "True" or stopped:
        if (
            resource.kind == "ModelService"
            and int(status.get("servingGeneration") or 0) != generation
        ):
            return ResourceProgress(
                resource, "Progressing", "Updating",
                "Waiting for the current ModelService generation to serve", False,
            )
        if not suspended and not alerts_selected and alerts_condition is not None:
            return ResourceProgress(
                resource, "Progressing", "AlertsRemoving",
                "Waiting for service alert removal", False,
            )
        if not suspended and alerts_selected and (
            not alerts_current or alerts_condition.get("status") != "True"
        ):
            return ResourceProgress(
                resource, "Progressing", "AlertsReconciling",
                "Waiting for the selected service alerts", False,
            )
        return ResourceProgress(resource, "Stopped" if stopped else "Ready", reason, message, True)
    if condition_status == "False" and reason in {
        "InvalidIntent", "ConfigurationRejected", "DeploymentRequired",
    }:
        compiled = next((item for item in conditions if item.get("type") == "IntentCompiled"), None)
        if compiled is not None and int(compiled.get("observedGeneration") or 0) == generation and compiled.get("status") == "False":
            message = str(compiled["message"])
        return ResourceProgress(resource, "Failed", reason, message, False)
    return ResourceProgress(resource, "Progressing", reason, message, False)


def namespace_progress(
    namespace: str, kubectl: Kubectl
) -> tuple[ResourceProgress, ...]:
    """Read every user-facing Foretoken service in one namespace."""
    progress: list[ResourceProgress] = []
    values = kubectl.list_resources(
        ("frontendservice", "modelservice"), namespace
    )
    for value in values:
        resource = resource_ref(value)
        if resource.kind in {"FrontendService", "ModelService"}:
            progress.append(resource_progress(resource, value))
    if not progress:
        raise DeploymentError(
            f"no FrontendService or ModelService resources found in namespace {namespace}"
        )
    return tuple(progress)


def read_progress(
    resources: Iterable[ResourceRef], kubectl: Kubectl,
    *, expected_revisions: dict[ResourceRef, str] | None = None,
) -> tuple[ResourceProgress, ...]:
    """Read named service readiness with one Kubernetes request."""
    selected = tuple(resources)
    values = kubectl.get_resources(selected)
    by_identity = {
        (resource.kind, resource.name): value
        for value in values
        for resource in (resource_ref(value),)
    }
    try:
        return tuple(
            resource_progress(
                resource, by_identity[(resource.kind, resource.name)],
                expected_revision=(expected_revisions or {}).get(resource),
            )
            for resource in selected
        )
    except KeyError as exc:
        raise DeploymentError("kubectl omitted a selected Foretoken service") from exc


def wait_for_resources(
    resources: tuple[ResourceRef, ...],
    kubectl: Kubectl,
    timeout: str,
    *,
    report: Callable[[float, ResourceProgress], None] | None = None,
    observe: Callable[[tuple[ResourceRef, ...], float, float], None] | None = None,
    expected_revisions: dict[ResourceRef, str] | None = None,
) -> tuple[ResourceProgress, ...]:
    """Wait until every service reaches its requested running or stopped state."""
    started = time.monotonic()
    previous: dict[ResourceRef, tuple[str, str, str]] = {}
    latest: tuple[ResourceProgress, ...] = ()
    with kubectl.until(time.monotonic() + timeout_seconds(timeout)) as deadline:
        observation_error = None
        while time.monotonic() < deadline:
            try:
                latest = read_progress(resources, kubectl, expected_revisions=expected_revisions)
            except DeploymentError as error:
                if time.monotonic() < deadline:
                    raise
                observation_error = error
                break
            elapsed = time.monotonic() - started
            if report is not None:
                for progress in latest:
                    signature = (progress.state, progress.reason, progress.message)
                    if previous.get(progress.resource) != signature:
                        report(elapsed, progress)
                        previous[progress.resource] = signature
            if all(progress.converged for progress in latest):
                return latest
            if any(progress.state == "Failed" for progress in latest):
                failed = next(progress for progress in latest if progress.state == "Failed")
                raise DeploymentError(
                    f"{failed.resource.display_name} failed: {failed.detail or failed.reason}"
                )
            if observe is not None:
                observe(resources, elapsed, max(0.0, deadline - time.monotonic()))
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
        pending = "; ".join(
            f"{item.resource.display_name}: {item.detail or item.state}"
            for item in latest if not item.converged
        ) or "no service status was observed"
        if observation_error is not None:
            pending += f"\nLast observation: {observation_error}"
        raise DeploymentError(
            f"serving readiness wait expired after {time.monotonic() - started:.1f}s "
            f"(budget {timeout}): {pending}"
        ) from observation_error


def report_progress(elapsed: float, progress: ResourceProgress) -> None:
    """Print a changed service state for deployment waits and status watches."""
    suffix = f" — {progress.detail}" if progress.detail else ""
    print(
        f"[{elapsed:6.1f}s] {progress.resource.display_name:<48} "
        f"{progress.state}{suffix}",
        flush=True,
    )


def _print_status(progress: tuple[ResourceProgress, ...]) -> None:
    """Print a point-in-time readiness table for selected services."""
    resource_width = max(
        len("RESOURCE"), *(len(item.resource.display_name) for item in progress)
    )
    state_width = max(len("STATUS"), *(len(item.state) for item in progress))
    print(f"{'RESOURCE':<{resource_width}}  {'STATUS':<{state_width}}  DETAILS")
    for item in progress:
        print(
            f"{item.resource.display_name:<{resource_width}}  "
            f"{item.state:<{state_width}}  {item.detail}"
        )


def show_status(command: StatusCommand) -> None:
    """Print CLI-selected service state, retaining log readers only for the observation lifetime."""
    kubectl = Kubectl()
    resources = (
        load_deployment(command.kustomize_path, kubectl).service_refs()
        if command.kustomize_path is not None else None
    )

    def selected_progress() -> tuple[ResourceProgress, ...]:
        """Read fixed deployment targets or the namespace's current services."""
        if resources is not None:
            return read_progress(resources, kubectl)
        return namespace_progress(command.namespace or "", kubectl)

    if not command.watch:
        progress = selected_progress()
        _print_status(progress)
        with StartupProgress(kubectl, lambda line: print(line, flush=True)) as startup:
            startup.poll((item.resource for item in progress), 0.0, follow_logs=False)
        return

    started = time.monotonic()
    previous: dict[ResourceRef, tuple[str, str, str]] = {}
    with StartupProgress(kubectl, lambda line: print(line, flush=True)) as startup:
        while True:
            progress = selected_progress()
            elapsed = time.monotonic() - started
            for item in progress:
                signature = (item.state, item.reason, item.message)
                if previous.get(item.resource) != signature:
                    report_progress(elapsed, item)
                    previous[item.resource] = signature
            startup.poll((item.resource for item in progress), elapsed)
            time.sleep(2)
