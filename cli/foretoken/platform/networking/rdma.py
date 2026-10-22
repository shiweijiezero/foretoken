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
        previous: list[dict[str, Any]],
        explicit: list[dict[str, str]] | None,
        timeout: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """Return retained definitions, the active selection and the installed IPAM path."""
        inspection = NodeInspection(
            self._kubectl, platform.namespace, image, pull_secrets
        )
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
            return self._retain_definitions(
                platform, previous, selected, timeout, inspection
            )

        script = Path(__file__).with_name("rdma_interfaces.py").read_text()
        observations = [
            inspection.run(node["metadata"]["name"], script, timeout) for node in nodes
        ]
        available = [value for value in observations if value["active_roce_ports"]]
        if not available:
            return self._retain_definitions(platform, previous, [], timeout, inspection)
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
        return self._retain_definitions(
            platform, previous, selected, timeout, inspection
        )

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
