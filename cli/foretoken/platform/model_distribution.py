# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare optional peer downloads through an internal Kubernetes Service."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

from foretoken.command_execution import CommandExecution
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, resource_ref
from foretoken.platform.helm import Helm
from foretoken.platform.types import ReleaseRef

_PROXY_SERVICE = "foretoken-model-downloads"
_PROXY_LABEL = "foretoken.io/model-download-proxy"
_PROXY_ARGUMENT = "--model-distribution-download-proxy="
_PROXY_ENV = "FORETOKEN_MODEL_DOWNLOAD_PROXY"


@dataclass(frozen=True)
class DragonflyPlan:
    """An installer-owned download service choice and its image credentials."""

    release: ReleaseRef | None
    action: str
    detail: str
    image_pull_secrets: tuple[dict[str, str], ...] = ()


class ModelDistributionLifecycle:
    """Own download discovery and its Service without taking over independent daemons."""

    def __init__(self, helm: Helm, kubectl: Kubectl) -> None:
        self._helm = helm
        self._kubectl = kubectl

    def resolve_install(self, values: tuple[dict[str, Any], ...]) -> DragonflyPlan:
        """Select an unambiguous service without making peer downloads a user setting."""
        image_pull_secrets: tuple[dict[str, str], ...] = ()
        for value in values:
            if "imagePullSecrets" in value:
                image_pull_secrets = tuple(value["imagePullSecrets"])
        release = self._helm.dragonfly_release()
        exists = self._helm.release_exists(release)
        if exists and self._helm.is_cli_managed(release):
            return DragonflyPlan(
                release, "Upgrade", release.display_name, image_pull_secrets
            )
        existing = self._existing_releases()
        if existing is None:
            return DragonflyPlan(
                None, "Skip", "node downloads have an independent non-Helm owner"
            )
        if len(existing) > 1:
            return DragonflyPlan(
                None,
                "Skip",
                "multiple node download services; use model providers directly",
            )
        if existing:
            return DragonflyPlan(existing[0], "Reuse", existing[0].display_name)
        if exists:
            return DragonflyPlan(
                None, "Skip", f"{release.display_name} belongs to another installation"
            )
        return DragonflyPlan(
            release, "Install", release.display_name, image_pull_secrets
        )

    def _existing_releases(self) -> tuple[ReleaseRef, ...] | None:
        """Find native services; a non-Helm daemon retains its independent lifecycle."""
        releases: set[ReleaseRef] = set()
        for daemon in self._kubectl.list_all_resources(("daemonset.apps",)):
            metadata = daemon["metadata"]
            for volume in daemon["spec"]["template"]["spec"].get("volumes", []):
                if "configMap" not in volume:
                    continue
                config = self._kubectl.get_if_exists(
                    "configmap", volume["configMap"]["name"], metadata["namespace"]
                )
                if config is None or "dfdaemon.yaml" not in config.get("data", {}):
                    continue
                ownership = metadata.get("annotations", {})
                name = ownership.get("meta.helm.sh/release-name")
                namespace = ownership.get("meta.helm.sh/release-namespace")
                if not name or not namespace:
                    return None
                releases.add(ReleaseRef(name, namespace))
        return tuple(releases)

    def apply(
        self, plan: DragonflyPlan, accelerator_resource: str, timeout: str
    ) -> str:
        """Prepare a network endpoint; workers use their provider when no peer can serve it."""
        if plan.release is None:
            return ""
        if plan.action != "Reuse" and not self._helm.install_dragonfly(
            plan.release, accelerator_resource, plan.image_pull_secrets, timeout
        ):
            return ""
        reference = self._helm.dragonfly_daemonset(plan.release)
        daemon = (
            self._kubectl.get_if_exists(
                reference.kind, reference.name, reference.namespace
            )
            if reference
            else None
        )
        config = (
            self._daemon_configuration(daemon, reference.namespace) if daemon else None
        )
        proxy = config.get("proxy") if isinstance(config, dict) else None
        server = proxy.get("server") if isinstance(proxy, dict) else None
        port = server.get("port") if isinstance(server, dict) else None
        supported = (
            type(port) is int and 0 < port <= 65535 and server.get("basicAuth") is None
        )
        if supported and server.get("ip"):
            try:
                supported = not ipaddress.ip_address(server["ip"]).is_loopback
            except ValueError:
                supported = False
        selector = daemon["spec"]["selector"] if daemon else {}
        if (
            not supported
            or not selector.get("matchLabels")
            or selector.get("matchExpressions")
        ):
            print(
                f"Model downloads: {plan.release.display_name} has no compatible endpoint; using model providers directly",
                flush=True,
            )
            return ""
        return self._prepare_proxy_service(
            reference.namespace, selector["matchLabels"], port
        )

    def _daemon_configuration(
        self, daemon: dict[str, Any], namespace: str
    ) -> dict[str, Any] | None:
        """Read the selected daemon's native configuration without guessing its defaults."""
        configurations = []
        for volume in daemon["spec"]["template"]["spec"].get("volumes", []):
            if "configMap" not in volume:
                continue
            config_map = self._kubectl.get_if_exists(
                "configmap", volume["configMap"]["name"], namespace
            )
            if config_map is None or "dfdaemon.yaml" not in config_map.get("data", {}):
                continue
            try:
                value = yaml.safe_load(config_map["data"]["dfdaemon.yaml"])
            except yaml.YAMLError:
                return None
            if not isinstance(value, dict):
                return None
            configurations.append(value)
        return configurations[0] if len(configurations) == 1 else None

    def _prepare_proxy_service(
        self, namespace: str, selector: dict[str, str], port: int
    ) -> str:
        """Own a stable internal Service while leaving the selected daemon's lifecycle unchanged."""
        current = self._kubectl.get_if_exists("service", _PROXY_SERVICE, namespace)
        owner_key, owner_value = self._helm.management_label
        labels = {owner_key: owner_value, _PROXY_LABEL: "true"}
        if current is not None and not all(
            current["metadata"].get("labels", {}).get(key) == value
            for key, value in labels.items()
        ):
            print(
                f"Model downloads: Service {namespace}/{_PROXY_SERVICE} has another owner; using model providers directly",
                flush=True,
            )
            return ""
        # Create is conditional on absence. Replace retains the observed UID/version
        # and server-assigned fields, including the allocated cluster address.
        operation = "create" if current is None else "replace"
        desired = (
            current
            if current is not None
            else {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": _PROXY_SERVICE,
                    "namespace": namespace,
                    "labels": labels,
                },
                "spec": {},
            }
        )
        desired["spec"].update(
            {
                "type": "ClusterIP",
                "selector": selector,
                "ports": [
                    {
                        "name": "proxy",
                        "protocol": "TCP",
                        "port": 80,
                        "targetPort": port,
                    }
                ],
            }
        )
        completed = self._kubectl.run(
            [operation, "--field-manager=foretoken-model-downloads", "--filename=-"],
            input_text=yaml.safe_dump(desired),
            check=False,
        )
        if execution := CommandExecution.current():
            execution.check()
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            print(
                f"Shared download Service is unavailable; using model providers directly: {detail}",
                flush=True,
            )
            return ""
        return self._proxy_url(namespace)

    @staticmethod
    def _proxy_url(namespace: str) -> str:
        """Return the stable address of Foretoken's namespace-local proxy Service."""
        return f"http://{_PROXY_SERVICE}.{namespace}.svc"

    @staticmethod
    def _pod_spec(resource: dict[str, Any]) -> dict[str, Any] | None:
        """Read current and declared consumers, excluding terminal workloads."""
        status = resource.get("status", {})
        if status.get("phase") in {"Succeeded", "Failed"} or any(
            condition["type"] in {"Complete", "Failed"}
            and condition["status"] == "True"
            for condition in status.get("conditions", [])
        ):
            return None
        return (
            resource["spec"]
            if resource["kind"] == "Pod"
            else resource["spec"]["template"]["spec"]
        )

    def finish_uninstall(
        self,
        timeout: str,
        *,
        keep_release: ReleaseRef | None = None,
        keep_proxy_url: str = "",
    ) -> tuple[str, str]:
        """Retire unused owned Services and daemons while preserving declared consumers."""
        owner_key, owner_value = self._helm.management_label
        proxies = self._kubectl.list_all_resources(
            ("service",),
            label_selector=f"{owner_key}={owner_value},{_PROXY_LABEL}=true",
        )
        release = self._helm.dragonfly_release()
        exists = self._helm.release_exists(release)
        managed = exists and self._helm.is_cleanup_managed(release)
        if not proxies and not managed:
            return "Skip", "no managed download resources"
        consumers = [
            (resource, spec)
            for resource in self._kubectl.list_all_resources(
                ("pod", "job.batch", "deployment.apps")
            )
            if (spec := self._pod_spec(resource)) is not None
        ]
        references = {keep_proxy_url} if keep_proxy_url else set()
        for _, spec in consumers:
            for container in spec["containers"]:
                references.update(
                    argument.removeprefix(_PROXY_ARGUMENT)
                    for argument in container.get("args", [])
                    if argument.startswith(_PROXY_ARGUMENT)
                )
                references.update(
                    variable["value"]
                    for variable in container.get("env", [])
                    if variable["name"] == _PROXY_ENV and variable.get("value")
                )
        retained = []
        for service in proxies:
            namespace = service["metadata"]["namespace"]
            if self._proxy_url(namespace) in references:
                retained.append(service)
            else:
                self._kubectl.delete_object(service, timeout)
        if not managed:
            return "Preserve", "independently managed download services"
        if keep_release == release:
            return "Preserve", release.display_name
        daemon_ref = self._helm.dragonfly_daemonset(release)
        daemon = (
            self._kubectl.get_if_exists(
                daemon_ref.kind, daemon_ref.name, daemon_ref.namespace
            )
            if daemon_ref
            else None
        )
        if daemon is not None:
            if any(
                service["metadata"]["namespace"] == daemon_ref.namespace
                and service["spec"].get("selector")
                == daemon["spec"]["selector"].get("matchLabels")
                for service in retained
            ):
                return (
                    "Preserve",
                    f"download clients still reference {release.display_name}",
                )
            # Older namespace controllers keep their socket transport until redeployment.
            config = self._daemon_configuration(daemon, daemon_ref.namespace)
            if config is None:
                raise DeploymentError(
                    f"Cannot determine the binding of {release.display_name} before cleanup"
                )
            configured = ((config.get("download") or {}).get("server") or {}).get(
                "socketPath"
            )
            if isinstance(configured, str):
                spec = daemon["spec"]["template"]["spec"]
                volumes = {volume["name"]: volume for volume in spec.get("volumes", [])}
                for container in spec["containers"]:
                    for mount in container.get("volumeMounts", []):
                        host = volumes[mount["name"]].get("hostPath")
                        if host is None or not PurePosixPath(configured).is_relative_to(
                            mount["mountPath"]
                        ):
                            continue
                        socket = PurePosixPath(host["path"]) / PurePosixPath(
                            configured
                        ).relative_to(mount["mountPath"])
                        mounts = {str(socket), str(socket.parent)}
                        argument = f"--model-distribution-dragonfly-socket={socket}"
                        for resource, consumer in consumers:
                            if any(
                                owner.get("uid") == daemon["metadata"]["uid"]
                                for owner in resource["metadata"].get(
                                    "ownerReferences", []
                                )
                            ):
                                continue
                            if any(
                                volume.get("hostPath", {}).get("path") in mounts
                                for volume in consumer.get("volumes", [])
                            ) or any(
                                argument in item.get("args", [])
                                for item in consumer["containers"]
                            ):
                                ref = resource_ref(resource)
                                return (
                                    "Preserve",
                                    f"{ref.namespace}/{ref.display_name} still uses Dragonfly",
                                )
        self._helm.uninstall(release, timeout)
        return "Removed", release.display_name
