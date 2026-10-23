# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare real RoCE networks alongside the platform's device allocation."""

from __future__ import annotations

import ipaddress
import json
import secrets
import uuid
from pathlib import Path
from typing import Any

from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl
from foretoken.platform.helm_client import owned_by_release
from foretoken.platform.networking.inspection import NodeInspection
from foretoken.platform.networking.multus import NAD_RESOURCE, MultusLifecycle
from foretoken.platform.types import ReleaseRef


class RDMANetworkLifecycle:
    """Select Pod attachments while retaining their definitions for the platform's lifetime."""

    def __init__(self, kubectl: Kubectl, multus: MultusLifecycle) -> None:
        self._kubectl = kubectl
        self._multus = multus

    def prepare(
        self,
        platform: ReleaseRef,
        nodes: tuple[dict[str, Any], ...],
        image: str,
        pull_secrets: list[dict[str, str]],
        runtime_pod: dict[str, Any],
        previous: list[dict[str, Any]],
        explicit: list[dict[str, str]] | None,
        timeout: str,
        *,
        resource_names: tuple[str, ...],
        resource_count: int,
        bindings: list[dict[str, Any]] | None,
        runtime_class_name: str,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        """Resolve resource/network alternatives and retain managed attachments until uninstall."""
        inspection = NodeInspection(
            self._kubectl,
            platform.namespace,
            image,
            pull_secrets,
            runtime_pod,
            runtime_class_name,
            tuple(node["metadata"]["name"] for node in nodes),
        )
        if bindings is not None:
            retained, ipam = self._retain_definitions(
                platform, previous, [], timeout, inspection
            )
            return retained, ipam, bindings
        if explicit is None and resource_names:
            existing = self._existing_bindings(resource_names, resource_count)
            if existing:
                retained, ipam = self._retain_definitions(
                    platform, previous, [], timeout, inspection
                )
                return retained, ipam, existing
        if explicit is not None:
            retained = {
                (value.get("namespace", platform.namespace), value["name"]): value
                for value in previous
            }
            selected = [
                retained.get(
                    (value.get("namespace", platform.namespace), value["name"]), value
                )
                for value in explicit
            ]
            for network in selected:
                if not network.get("master"):
                    self._kubectl.get(
                        NAD_RESOURCE,
                        network["name"],
                        network.get("namespace", platform.namespace),
                    )
            if selected and len(resource_names) > 1:
                raise DeploymentError(
                    "multiple RDMA pools require network/resource associations in rdma.bindings"
                )
            retained, ipam = self._retain_definitions(
                platform, previous, selected, timeout, inspection
            )
            return (
                retained,
                ipam,
                self._bindings(platform, resource_names, resource_count, selected),
            )

        script = Path(__file__).with_name("rdma_interfaces.py").read_text()
        observations = [
            inspection.run(node["metadata"]["name"], script, timeout) for node in nodes
        ]
        available = [value for value in observations if value["active_roce_ports"]]
        if not available:
            retained, ipam = self._retain_definitions(
                platform, previous, [], timeout, inspection
            )
            return (
                retained,
                ipam,
                self._bindings(platform, resource_names, resource_count, []),
            )
        if len(resource_names) > 1:
            raise DeploymentError(
                "multiple RoCE pools have no resource-associated NetworkAttachmentDefinitions; "
                "retain their network owner's allocation mapping"
            )
        # The shared allocation can select any RDMA node, not only the RoCE subset.
        incompatible = [
            node["metadata"]["name"]
            for node, value in zip(nodes, observations, strict=True)
            if value["rdma_ports"] and not value["active_roce_ports"]
        ]
        if incompatible:
            raise DeploymentError(
                "GPU nodes without an active RoCE fabric share the RDMA allocation: "
                + ", ".join(incompatible)
                + "; select one fabric with runtime.vllm.gpu.nodeSelector or provide rdma.networks"
            )
        if any(not value["interfaces"] for value in available):
            raise DeploymentError(
                "RDMA devices are visible but a GPU node has no usable RoCE network interface; "
                "prepare its node network before installing the platform"
            )
        # A shared NAD must name the same underlying fabric on every selected node.
        interfaces = [
            {entry["master"]: entry for entry in value["interfaces"]}
            for value in available
        ]
        masters = set(interfaces[0])
        if any(set(value) != masters for value in interfaces[1:]):
            raise DeploymentError(
                "GPU nodes have different RoCE interface layouts; select existing network attachments with rdma.networks"
            )
        for master in masters:
            fabrics = [
                value[master]["prefixes"]
                for value in interfaces
                if not value[master]["node_anchor"]
            ]
            if fabrics and (
                not fabrics[0] or any(value != fabrics[0] for value in fabrics[1:])
            ):
                raise DeploymentError(
                    f"RoCE interface {master} does not identify one shared fabric; select its attachment with rdma.networks"
                )
        retained = {
            network["master"]: network for network in previous if network.get("master")
        }
        selected = []
        for master in sorted(masters):
            if master in retained:
                selected.append(retained[master])
                continue
            prefix = ipaddress.IPv6Network(
                (int.from_bytes(b"\xfd" + secrets.token_bytes(7) + bytes(8), "big"), 64)
            )
            selected.append(
                {
                    "name": "foretoken-rdma-" + uuid.uuid4().hex[:12],
                    "master": master,
                    "subnet": str(prefix),
                }
            )
        retained, ipam = self._retain_definitions(
            platform, previous, selected, timeout, inspection
        )
        return (
            retained,
            ipam,
            self._bindings(platform, resource_names, resource_count, selected),
        )

    def _existing_bindings(
        self,
        resource_names: tuple[str, ...],
        count: int,
    ) -> list[dict[str, Any]]:
        """Reuse resource-associated NADs for known RDMA pools without rewriting their CNI/IPAM policy."""
        if NAD_RESOURCE not in self._kubectl.api_resource_names("k8s.cni.cncf.io"):
            return []
        bindings = []
        for network in self._kubectl.list_all_resources((NAD_RESOURCE,)):
            metadata = network["metadata"]
            resource = metadata.get("annotations", {}).get(
                "k8s.v1.cni.cncf.io/resourceName"
            )
            if resource not in resource_names or metadata.get("deletionTimestamp"):
                continue
            namespace = metadata["namespace"]
            bindings.append(
                {
                    "namespace": namespace,
                    "allocation": {
                        "resources": {resource: str(count)},
                        "networks": [f"{namespace}/{metadata['name']}"],
                    },
                }
            )
        return sorted(bindings, key=lambda value: value["allocation"]["networks"])

    @staticmethod
    def _bindings(
        platform: ReleaseRef,
        resource_names: tuple[str, ...],
        count: int,
        networks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Associate installer-owned attachments with each alternative shared allocation."""
        references = [
            f"{network.get('namespace', platform.namespace)}/{network['name']}"
            for network in networks
        ]
        return [
            {"allocation": {"resources": {name: str(count)}, "networks": references}}
            for name in resource_names
        ]

    def _retain_definitions(
        self,
        platform: ReleaseRef,
        previous: list[dict[str, Any]],
        selected: list[dict[str, Any]],
        timeout: str,
        inspection: NodeInspection,
    ) -> tuple[list[dict[str, Any]], str]:
        """Keep managed NADs until uninstall so existing and concurrent instance selections remain valid."""
        identities = {
            (value.get("namespace", platform.namespace), value["name"])
            for value in selected
        }
        networks = [{**value, "active": True} for value in selected]
        networks.extend(
            {**value, "active": False}
            for value in previous
            if value.get("master")
            and (value.get("namespace", platform.namespace), value["name"])
            not in identities
        )
        ipam = (
            self._multus.ensure(timeout, inspection)
            if any(value.get("master") for value in networks)
            else ""
        )
        return networks, ipam

    def prepare_uninstall(self, platform: ReleaseRef) -> tuple[dict[str, Any], ...]:
        """Find unused platform-owned NADs, including definitions retained through Helm rollback."""
        if NAD_RESOURCE not in self._kubectl.api_resource_names("k8s.cni.cncf.io"):
            return ()
        networks = tuple(
            value
            for value in self._kubectl.list_resources(
                (NAD_RESOURCE,), platform.namespace
            )
            if owned_by_release(value, platform)
        )
        managed = {
            (value["metadata"]["namespace"], value["metadata"]["name"])
            for value in networks
        }
        if not managed:
            return ()
        for pod in self._kubectl.list_all_resources(("pod",)):
            metadata = pod["metadata"]
            raw = metadata.get("annotations", {}).get("k8s.v1.cni.cncf.io/networks", "")
            if not raw:
                continue
            attachments = (
                json.loads(raw)
                if raw.lstrip().startswith("[")
                else [name.strip() for name in raw.split(",")]
            )
            for attachment in attachments:
                if isinstance(attachment, str):
                    parts = attachment.split("@", 1)[0].split("/", 1)
                    identity = (
                        (metadata["namespace"], parts[0])
                        if len(parts) == 1
                        else tuple(parts)
                    )
                else:
                    identity = (
                        attachment.get("namespace", metadata["namespace"]),
                        attachment["name"],
                    )
                if identity in managed:
                    raise DeploymentError(
                        f"cannot retire RoCE attachment {identity[0]}/{identity[1]} while Pod {metadata['namespace']}/{metadata['name']} uses it"
                    )
        return networks
