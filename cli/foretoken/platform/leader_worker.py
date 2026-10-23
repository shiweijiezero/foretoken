# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Reuse or manage the cluster's LeaderWorkerSet controller for model groups."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, ResourceRef, resource_ref
from foretoken.platform.helm_client import HelmClient
from foretoken.platform.types import ReleaseRef

_API_GROUP = "leaderworkerset.x-k8s.io"
_RESOURCE = f"leaderworkersets.{_API_GROUP}"
_KUEUE_API_GROUP = "kueue.x-k8s.io"


@dataclass(frozen=True)
class LeaderWorkerScheduling:
    """Select independent placement and quota queues for distributed model groups."""

    volcano_queue_name: str = ""
    kueue_local_queue_name: str = ""


def scheduling_from_values(values: tuple[dict[str, Any], ...]) -> LeaderWorkerScheduling | None:
    """Resolve the platform's ordered Helm selection without exposing scheduler state to model users."""
    selected: dict[str, Any] = {}
    for item in values:
        workload = item.get("workload", {})
        if not isinstance(workload, dict):
            raise DeploymentError("workload must be a mapping")
        if "leaderWorkerScheduling" not in workload:
            continue
        configuration = workload["leaderWorkerScheduling"]
        if configuration is None:
            selected = {}
        elif not isinstance(configuration, dict):
            raise DeploymentError("workload.leaderWorkerScheduling must be a mapping or null")
        else:
            selected.update(configuration)
    if selected.keys() - {"volcanoQueueName", "kueueLocalQueueName"}:
        raise DeploymentError("workload.leaderWorkerScheduling supports volcanoQueueName and kueueLocalQueueName")
    for field, maximum in (("volcanoQueueName", 253), ("kueueLocalQueueName", 63)):
        queue = selected.get(field)
        if queue is None:
            selected[field] = ""
        elif not isinstance(queue, str) or (queue and (len(queue) > maximum or re.fullmatch(
            r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*", queue,
        ) is None)):
            raise DeploymentError(f"workload.leaderWorkerScheduling.{field} must be a native Kubernetes queue name")
    if not any(selected.values()):
        return None
    return LeaderWorkerScheduling(selected["volcanoQueueName"], selected["kueueLocalQueueName"])


@dataclass(frozen=True)
class LeaderWorkerPlan:
    """Resolved controller ownership and operation for platform installation."""

    release: ReleaseRef
    action: str
    detail: str
    controller: ResourceRef | None = None
    scheduling: LeaderWorkerScheduling | None = None


class LeaderWorkerLifecycle:
    """Own the managed LWS release while leaving shared controllers and CRDs intact."""

    def __init__(self, helm: HelmClient, kubectl: Kubectl) -> None:
        self._helm = helm
        self._kubectl = kubectl
        self._release = ReleaseRef(helm.config.leader_worker.release_name, helm.config.namespace)

    def resolve_install(self, values: tuple[dict[str, Any], ...] = ()) -> LeaderWorkerPlan:
        """Select the native controller and queue dependencies without changing external installations."""
        scheduling = scheduling_from_values(values)
        if scheduling is not None:
            self._require_scheduling_api(scheduling)
        release = self._release
        release_exists = self._helm.release_exists(release)
        if release_exists and self._helm.is_cli_managed(release):
            return LeaderWorkerPlan(release, "Upgrade", release.display_name, scheduling=scheduling)

        # Webhook service selectors identify the actual controller, including
        # installations whose namespace, release name, or image was customized.
        services: set[tuple[str, str]] = set()
        for configuration in self._kubectl.list_cluster_resources(
            ("mutatingwebhookconfiguration", "validatingwebhookconfiguration")
        ):
            # Kueue also admits LWS objects; its Workload hooks identify that separate owner.
            if any(_webhook_handles(webhook, _KUEUE_API_GROUP, "workloads") for webhook in configuration.get("webhooks", [])):
                continue
            for webhook in configuration.get("webhooks", []):
                if not _webhook_handles(webhook, _API_GROUP, "leaderworkersets"):
                    continue
                service = webhook.get("clientConfig", {}).get("service")
                if service is None:
                    raise DeploymentError(
                        "LeaderWorkerSet uses an external webhook URL; "
                        "verify its controller through its existing installation lifecycle"
                    )
                services.add((service["namespace"], service["name"]))
        controllers: set[ResourceRef] = set()
        for namespace, name in services:
            service = self._kubectl.get("service", name, namespace)
            selector = service["spec"].get("selector", {})
            if not selector:
                raise DeploymentError(
                    f"LeaderWorkerSet webhook service {namespace}/{name} has no selector"
                )
            for deployment in self._kubectl.list_resources(
                ("deployment.apps",), namespace
            ):
                labels = deployment["spec"]["template"]["metadata"].get("labels", {})
                if all(labels.get(key) == value for key, value in selector.items()):
                    controllers.add(resource_ref(deployment))
        if services and len(controllers) != 1:
            raise DeploymentError(
                "LeaderWorkerSet webhooks must select one existing controller Deployment"
            )
        if controllers:
            controller = next(iter(controllers))
            self._require_api()
            if scheduling is not None and scheduling.volcano_queue_name:
                self._require_volcano_controller(controller)
            return LeaderWorkerPlan(
                release, "Reuse", f"{controller.namespace}/{controller.display_name}", controller, scheduling,
            )
        if release_exists:
            raise DeploymentError(
                f"Helm release {release.display_name} is not managed by foretoken "
                "and has no reusable LeaderWorkerSet controller"
            )
        return LeaderWorkerPlan(release, "Install", release.display_name, scheduling=scheduling)

    def apply(self, plan: LeaderWorkerPlan, timeout: str) -> None:
        """Prepare a ready controller and served API before the Foretoken controller starts."""
        if plan.controller is not None:
            self._kubectl.rollout_status(plan.controller, timeout)
            return
        helm = self._helm
        chart = helm.config.leader_worker
        values = helm.stored_chart_values(plan.release)
        # Retained applications can still resume Volcano groups after platform defaults change.
        if plan.scheduling is not None and plan.scheduling.volcano_queue_name:
            values["gangSchedulingManagement"] = {"schedulerProvider": "volcano"}
        with helm.prepared_chart(chart.source, chart.version) as prepared:
            if plan.action == "Upgrade":
                # Helm upgrades leave CRDs unchanged; apply this chart's definitions first.
                self._kubectl.apply(helm.run(["show", "crds", str(prepared)]).stdout)
            args = helm.upgrade_args(plan.release, str(prepared), timeout)
            helm.set_chart_image_sources(prepared, ("image.manager",), release=plan.release, overrides=values)
            args.extend(["--reset-values", "--values", "-"])
            helm.run(args, input_text=json.dumps(values))
        self._kubectl.wait_for_crds((_RESOURCE,), timeout)
        self._require_api()

    def finish_uninstall(self, timeout: str) -> tuple[str, str]:
        """Remove our controller only when no LWS-managed custom resources remain."""
        release = self._release
        if not self._helm.release_exists(release):
            return "Preserve", "no CLI-managed controller release"
        if not self._helm.is_cleanup_managed(release):
            return "Preserve", release.display_name
        kinds = tuple(
            name
            for group in (_API_GROUP, "disaggregatedset.x-k8s.io")
            for name in self._kubectl.api_resource_names(group)
        )
        users = self._kubectl.list_all_resources(kinds) if kinds else ()
        if users:
            return "Preserve", "LeaderWorkerSet resources still use the controller"
        self._helm.uninstall(release, timeout)
        return "Removed", release.display_name

    def _require_scheduling_api(self, selection: LeaderWorkerScheduling) -> None:
        """Resolve native queue infrastructure without installing or adopting its controllers."""
        for queue, group, resources in (
            (selection.volcano_queue_name, "scheduling.volcano.sh", {"queues", "podgroups"}),
            (selection.kueue_local_queue_name, _KUEUE_API_GROUP, {"localqueues", "clusterqueues", "workloads"}),
        ):
            if queue and not {f"{resource}.{group}" for resource in resources}.issubset(self._kubectl.api_resource_names(group)):
                raise DeploymentError(f"leader-worker scheduling requires the native queue APIs in {group}")
        if selection.volcano_queue_name:
            self._kubectl.get("queues.scheduling.volcano.sh", selection.volcano_queue_name)

    def _require_volcano_controller(self, controller: ResourceRef) -> None:
        """Read the external controller's mounted native configuration without changing it."""
        deployment = self._kubectl.get("deployment.apps", controller.name, controller.namespace)
        pod = deployment["spec"]["template"]["spec"]
        volumes = {volume["name"]: volume for volume in pod.get("volumes", [])}
        for container in pod["containers"]:
            arguments = container.get("args", [])
            path = next((value.partition("=")[2] for value in arguments if value.startswith("--config=")), "")
            if not path and "--config" in arguments:
                path = arguments[arguments.index("--config") + 1]
            for mount in container.get("volumeMounts", []):
                source = volumes[mount["name"]].get("configMap")
                if source is None or not path:
                    continue
                target = PurePosixPath(mount["mountPath"])
                if mount.get("subPath") and target == PurePosixPath(path):
                    selected = mount["subPath"]
                elif not mount.get("subPath") and PurePosixPath(path).is_relative_to(target):
                    selected = str(PurePosixPath(path).relative_to(target))
                else:
                    continue
                key = next((item["key"] for item in source.get("items", []) if item["path"] == selected), selected)
                configuration = self._kubectl.get("configmap", source["name"], controller.namespace)
                try:
                    settings = yaml.safe_load(configuration["data"][key])
                except yaml.YAMLError as error:
                    raise DeploymentError("the external LeaderWorkerSet controller has invalid configuration") from error
                if settings.get("gangSchedulingManagement", {}).get("schedulerProvider") == "volcano":
                    return
        raise DeploymentError(
            "the external LeaderWorkerSet controller must enable "
            "gangSchedulingManagement.schedulerProvider=volcano through its administrator"
        )

    def _require_api(self) -> None:
        """Require the v1 API consumed by the model-group controller."""
        crd = self._kubectl.get("customresourcedefinition", _RESOURCE)
        if not any(
            version["name"] == "v1" and version.get("served")
            for version in crd["spec"]["versions"]
        ):
            raise DeploymentError(
                "the installed LeaderWorkerSet controller must serve "
                "leaderworkerset.x-k8s.io/v1"
            )


def _webhook_handles(webhook: dict[str, Any], group: str, resource: str) -> bool:
    return any(group in rule.get("apiGroups", []) and resource in rule.get("resources", [])
               for rule in webhook.get("rules", []))
