# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Reuse or manage the cluster's LeaderWorkerSet controller for model groups."""

from __future__ import annotations

from dataclasses import dataclass

from foretoken.kubernetes import Kubectl, resource_ref
from foretoken.manifest import DeploymentError, ResourceRef
from foretoken.platform.helm import Helm
from foretoken.platform.types import ReleaseRef


_API_GROUP = "leaderworkerset.x-k8s.io"
_RESOURCE = f"leaderworkersets.{_API_GROUP}"


@dataclass(frozen=True)
class LeaderWorkerPlan:
    """Resolved controller ownership and operation for platform installation."""

    release: ReleaseRef
    action: str
    detail: str
    controller: ResourceRef | None = None


class LeaderWorkerLifecycle:
    """Own the managed LWS release while leaving shared controllers and CRDs intact."""

    def __init__(self, helm: Helm, kubectl: Kubectl) -> None:
        self._helm = helm
        self._kubectl = kubectl

    def resolve_install(self) -> LeaderWorkerPlan:
        """Select an existing controller or the CLI-owned release before installation."""
        release = self._helm.leader_worker_release()
        release_exists = self._helm.release_exists(release)
        if release_exists and self._helm.is_cli_managed(release):
            return LeaderWorkerPlan(release, "Upgrade", release.display_name)

        # Webhook service selectors identify the actual controller, including
        # installations whose namespace, release name, or image was customized.
        services: set[tuple[str, str]] = set()
        for configuration in self._kubectl.list_cluster_resources(
            ("mutatingwebhookconfiguration", "validatingwebhookconfiguration")
        ):
            for webhook in configuration.get("webhooks", []):
                if not any(
                    _API_GROUP in rule.get("apiGroups", [])
                    and "leaderworkersets" in rule.get("resources", [])
                    for rule in webhook.get("rules", [])
                ):
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
            return LeaderWorkerPlan(
                release, "Reuse", f"{controller.namespace}/{controller.display_name}", controller
            )
        if release_exists:
            raise DeploymentError(
                f"Helm release {release.display_name} is not managed by foretoken "
                "and has no reusable LeaderWorkerSet controller"
            )
        return LeaderWorkerPlan(release, "Install", release.display_name)

    def apply(self, plan: LeaderWorkerPlan, timeout: str) -> None:
        """Prepare a ready controller and served API before the Foretoken controller starts."""
        if plan.controller is not None:
            self._kubectl.rollout_status(plan.controller, timeout)
            return
        if plan.action == "Upgrade":
            # Helm deliberately leaves CRDs unchanged on upgrade. Apply only for
            # our owned release, without taking fields from another field manager.
            self._kubectl.apply(self._helm.leader_worker_crds())
        self._helm.install_leader_worker(plan.release, timeout)
        self._kubectl.wait_for_crds((_RESOURCE,), timeout)
        self._require_api()

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

    def finish_uninstall(self, timeout: str) -> tuple[str, str]:
        """Remove our controller only when no LWS-managed custom resources remain."""
        release = self._helm.leader_worker_release()
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
