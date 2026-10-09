# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Manage or reuse Dragonfly file distribution without changing node networking."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

from foretoken.kubernetes import Kubectl, resource_ref
from foretoken.manifest import DeploymentError
from foretoken.platform.helm import Helm
from foretoken.platform.types import ReleaseRef


@dataclass(frozen=True)
class DragonflyConfig:
    """The platform's P2P choice and optional independently managed release."""

    enabled: bool = False
    existing_release: ReleaseRef | None = None
    image_pull_secrets: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class DragonflyPlan:
    """A resolved release operation used by one platform installation."""

    config: DragonflyConfig
    release: ReleaseRef
    action: str


def dragonfly_config_from_values(values: tuple[dict[str, Any], ...]) -> DragonflyConfig:
    """Resolve the explicit P2P choice from stored values followed by current overrides."""
    enabled = False
    existing: dict[str, str] = {}
    image_pull_secrets: tuple[dict[str, str], ...] = ()
    for item in values:
        if "imagePullSecrets" in item:
            image_pull_secrets = tuple(item["imagePullSecrets"])
        distribution = item.get("modelDistribution", {})
        if not isinstance(distribution, dict):
            raise DeploymentError("modelDistribution must be a mapping")
        dragonfly = distribution.get("dragonfly", {})
        if not isinstance(dragonfly, dict):
            raise DeploymentError("modelDistribution.dragonfly must be a mapping")
        if "enabled" in dragonfly:
            enabled = dragonfly["enabled"]
            if not isinstance(enabled, bool):
                raise DeploymentError("modelDistribution.dragonfly.enabled must be a boolean")
        if "existingRelease" in dragonfly:
            value = dragonfly["existingRelease"]
            if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
                raise DeploymentError("modelDistribution.dragonfly.existingRelease must contain string fields")
            existing.update(value)
    name, namespace = existing.get("name", ""), existing.get("namespace", "")
    if bool(name) != bool(namespace):
        raise DeploymentError("Dragonfly existingRelease name and namespace must be set together")
    return DragonflyConfig(enabled, ReleaseRef(name, namespace) if name else None, image_pull_secrets)


class ModelDistributionLifecycle:
    """Own only the CLI-managed Dragonfly release; preserve an explicit shared release."""

    def __init__(self, helm: Helm, kubectl: Kubectl) -> None:
        self._helm = helm
        self._kubectl = kubectl

    def resolve_install(self, config: DragonflyConfig) -> DragonflyPlan:
        """Choose install, upgrade, reuse, or disable before changing the platform."""
        release = config.existing_release or self._helm.dragonfly_release()
        if not config.enabled:
            return DragonflyPlan(config, release, "Disabled")
        exists = self._helm.release_exists(release)
        if config.existing_release is not None:
            if not exists:
                raise DeploymentError(f"Dragonfly release {release.display_name} does not exist")
            return DragonflyPlan(config, release, "Reuse")
        if not exists:
            existing = self._existing_releases()
            if len(existing) > 1:
                raise DeploymentError("multiple Dragonfly releases exist; select modelDistribution.dragonfly.existingRelease")
            if existing:
                return DragonflyPlan(config, existing[0], "Reuse")
        if exists and not self._helm.is_cli_managed(release):
            raise DeploymentError(
                f"Helm release {release.display_name} is not managed by foretoken; "
                "select it with modelDistribution.dragonfly.existingRelease"
            )
        return DragonflyPlan(config, release, "Upgrade" if exists else "Install")

    def _existing_releases(self) -> tuple[ReleaseRef, ...]:
        """Find native daemon installations before another DaemonSet can claim their sockets."""
        releases: set[ReleaseRef] = set()
        for daemon in self._kubectl.list_all_resources(("daemonset.apps",)):
            metadata = daemon["metadata"]
            for volume in daemon["spec"]["template"]["spec"].get("volumes", []):
                if "configMap" not in volume:
                    continue
                config = self._kubectl.get_if_exists("configmap", volume["configMap"]["name"], metadata["namespace"])
                if config is None or "dfdaemon.yaml" not in config.get("data", {}):
                    continue
                ownership = metadata.get("annotations", {})
                name = ownership.get("meta.helm.sh/release-name")
                namespace = ownership.get("meta.helm.sh/release-namespace")
                if not name or not namespace:
                    raise DeploymentError("an existing Dragonfly daemon has no Helm owner; retain its existing installation lifecycle")
                releases.add(ReleaseRef(name, namespace))
        return tuple(releases)

    def apply(
        self, plan: DragonflyPlan, accelerator_resource: str, timeout: str,
        *, node_names: tuple[str, ...] = (),
    ) -> str:
        """Prepare a ready daemon and return its actual host socket for worker projection."""
        if not plan.config.enabled:
            return ""
        if plan.action != "Reuse":
            self._helm.install_dragonfly(plan.release, accelerator_resource, plan.config.image_pull_secrets, timeout)
        daemon = self._helm.dragonfly_daemonset(plan.release)
        self._kubectl.rollout_status(daemon, timeout)
        live = self._kubectl.get(daemon.kind, daemon.name, daemon.namespace)
        if (live.get("status") or {}).get("desiredNumberScheduled", 0) == 0:
            raise DeploymentError(f"Dragonfly DaemonSet {daemon.display_name} has no scheduled nodes")
        if node_names:
            ready_nodes = {
                pod["spec"]["nodeName"]
                for pod in self._kubectl.list_resources(("pod",), daemon.namespace)
                if any(owner.get("uid") == live["metadata"]["uid"] for owner in pod["metadata"].get("ownerReferences", []))
                and any(condition["type"] == "Ready" and condition["status"] == "True" for condition in pod.get("status", {}).get("conditions", []))
            }
            missing = set(node_names) - ready_nodes
            if missing:
                raise DeploymentError("Dragonfly has no ready client on selected accelerator nodes: " + ", ".join(sorted(missing)))
        return self._daemon_socket(live, daemon.namespace)

    def _daemon_socket(self, daemon: dict[str, Any], namespace: str) -> str:
        """Map the daemon's configured Unix socket through its actual hostPath mount."""
        spec = daemon["spec"]["template"]["spec"]
        volumes = {volume["name"]: volume for volume in spec.get("volumes", [])}
        sockets: set[str] = set()
        for container in spec["containers"]:
            mounts = container.get("volumeMounts", [])
            for mount in mounts:
                volume = volumes[mount["name"]]
                if "configMap" not in volume:
                    continue
                config_map = self._kubectl.get("configmap", volume["configMap"]["name"], namespace)
                raw = config_map.get("data", {}).get("dfdaemon.yaml")
                if raw is None:
                    continue
                try:
                    config = yaml.safe_load(raw)
                except yaml.YAMLError as exc:
                    raise DeploymentError("Dragonfly dfdaemon.yaml is invalid") from exc
                configured = ((config or {}).get("download") or {}).get("server", {}).get("socketPath")
                if not isinstance(configured, str) or not PurePosixPath(configured).is_absolute():
                    raise DeploymentError("Dragonfly must configure an absolute download socketPath")
                for socket_mount in mounts:
                    host = volumes[socket_mount["name"]].get("hostPath")
                    if host is None:
                        continue
                    try:
                        relative = PurePosixPath(configured).relative_to(socket_mount["mountPath"])
                    except ValueError:
                        continue
                    socket = PurePosixPath(host["path"]) / relative
                    if socket.parent == PurePosixPath("/") or ".." in socket.parts:
                        raise DeploymentError("Dragonfly download socket must use a dedicated host directory")
                    sockets.add(str(socket))
        if len(sockets) != 1:
            raise DeploymentError("Dragonfly release must expose one download socket through a hostPath directory")
        return sockets.pop()

    def finish_uninstall(self, timeout: str) -> tuple[str, str]:
        """Remove our daemon only when no remaining workload mounts its socket directory."""
        release = self._helm.dragonfly_release()
        if not self._helm.release_exists(release):
            return "Preserve", "no CLI-managed Dragonfly release"
        if not self._helm.is_cleanup_managed(release):
            return "Preserve", release.display_name
        daemon_ref = self._helm.dragonfly_daemonset(release)
        daemon = self._kubectl.get_if_exists(daemon_ref.kind, daemon_ref.name, daemon_ref.namespace)
        if daemon is not None:
            socket = self._daemon_socket(daemon, daemon_ref.namespace)
            socket_mounts = {socket, str(PurePosixPath(socket).parent)}
            for resource in self._kubectl.list_all_resources(("pod", "job.batch")):
                status = resource.get("status", {})
                if status.get("phase") in {"Succeeded", "Failed"} or any(
                    condition["type"] in {"Complete", "Failed"} and condition["status"] == "True"
                    for condition in status.get("conditions", [])
                ):
                    continue
                if any(owner.get("uid") == daemon["metadata"]["uid"] for owner in resource["metadata"].get("ownerReferences", [])):
                    continue
                spec = resource["spec"]["template"]["spec"] if resource["kind"] == "Job" else resource["spec"]
                if any(volume.get("hostPath", {}).get("path") in socket_mounts for volume in spec.get("volumes", [])):
                    ref = resource_ref(resource)
                    return "Preserve", f"{ref.namespace}/{ref.display_name} still uses Dragonfly"
        self._helm.uninstall(release, timeout)
        return "Removed", release.display_name
