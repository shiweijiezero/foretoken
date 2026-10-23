# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Reuse or manage Multus and its shared address allocator for secondary networks."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from foretoken.downloads.sources import select_github_download
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, resource_ref, timeout_seconds
from foretoken.platform.helm_client import HelmClient, merge_values, owned_by_release
from foretoken.platform.networking.inspection import NodeInspection
from foretoken.platform.types import ReleaseRef

NAD_RESOURCE = "network-attachment-definitions.k8s.cni.cncf.io"
_CRDS = (
    (
        NAD_RESOURCE,
        (
            "https://github.com/k8snetworkplumbingwg/network-attachment-definition-client/"
            "raw/refs/tags/v1.7.6/artifacts/networks-crd.yaml"
        ),
    ),
    (
        "ippools.whereabouts.cni.cncf.io",
        (
            "https://github.com/k8snetworkplumbingwg/whereabouts/raw/refs/tags/v0.9.4/"
            "deployment/whereabouts-chart/crds/whereabouts.cni.cncf.io_ippools.yaml"
        ),
    ),
    (
        "overlappingrangeipreservations.whereabouts.cni.cncf.io",
        (
            "https://github.com/k8snetworkplumbingwg/whereabouts/raw/refs/tags/v0.9.4/"
            "deployment/whereabouts-chart/crds/whereabouts.cni.cncf.io_overlappingrangeipreservations.yaml"
        ),
    ),
)
_IMAGE_PATHS = (
    "image",
    "cniplugins.image",
    "thickPlugin.image",
    "dynamicNetworksController.image",
    "dhcpDaemonSet.image",
    "rke2-whereabouts.image",
)
_NETWORK_ANNOTATION = "k8s.v1.cni.cncf.io/networks"


class MultusLifecycle:
    """Own one shared Multus release without replacing external CNI installations."""

    def __init__(self, helm: HelmClient, kubectl: Kubectl) -> None:
        self._helm = helm
        self._kubectl = kubectl
        self._release = ReleaseRef(helm.config.multus.release_name, "kube-system")

    def ensure(self, timeout: str, inspection: NodeInspection) -> str:
        """Return the installed shared IPAM path before callers create platform NADs.

        External agents retain their owner and namespace permissions. Every
        observed node must expose the same usable Whereabouts configuration.
        """
        helm, kubectl, release = self._helm, self._kubectl, self._release
        exists = helm.release_exists(release)
        managed = exists and helm.is_cli_managed(release)
        agents = tuple(
            value
            for value in kubectl.list_all_resources(("daemonset.apps",))
            if _is_multus(value)
        )
        if not managed and agents:
            self._ensure_apis(timeout, install=False)
            for agent in agents:
                kubectl.rollout_status(resource_ref(agent), timeout)
            return self._installed_ipam(agents, timeout, inspection)
        if exists and not managed:
            raise DeploymentError(
                f"Helm release {release.display_name} is not managed by foretoken "
                "and has no reusable Multus node agent"
            )
        external = tuple(
            value for value in agents if not owned_by_release(value, release)
        )
        if external:
            names = ", ".join(
                f"{value['metadata']['namespace']}/{value['metadata']['name']}"
                for value in external
            )
            raise DeploymentError(
                f"Multus node agents outside {release.display_name} already exist: {names}; "
                "retain their existing CNI installation lifecycle"
            )
        values = merge_values(
            helm.stored_chart_values(release), {"rke2-whereabouts": {"enabled": True}}
        )
        directories = tuple(
            inspection.runtime_paths(node, timeout)
            for node in kubectl.list_cluster_resources(("node",))
        )
        defaults = _cni_defaults(directories, values)
        self._ensure_apis(timeout, install=True)
        chart = helm.config.multus
        with helm.prepared_chart(chart.source, chart.version) as prepared:
            helm.set_chart_defaults(prepared, defaults)
            helm.set_chart_image_sources(
                prepared,
                _IMAGE_PATHS,
                release=release,
                overrides=values,
                subcharts=("rke2-whereabouts",),
            )
            args = helm.upgrade_args(release, str(prepared), timeout)
            args.extend(["--reset-values", "--values", "-"])
            helm.run(args, input_text=json.dumps(values))
        installed = tuple(
            agent
            for agent in kubectl.list_resources(("daemonset.apps",), release.namespace)
            if owned_by_release(agent, release)
        )
        for agent in installed:
            kubectl.rollout_status(resource_ref(agent), timeout)
        return self._installed_ipam(
            tuple(agent for agent in installed if _is_multus(agent)),
            timeout,
            inspection,
        )

    def _installed_ipam(
        self,
        agents: tuple[dict[str, Any], ...],
        timeout: str,
        inspection: NodeInspection,
    ) -> str:
        """Read installed node CNI files and require access to the platform's shared NADs."""
        paths: set[str] = set()
        for agent in agents:
            namespace = agent["metadata"]["namespace"]
            pods = tuple(
                pod
                for pod in self._kubectl.list_resources(("pods",), namespace)
                if pod.get("status", {}).get("phase") == "Running"
                and not pod["metadata"].get("deletionTimestamp")
                and any(
                    owner["uid"] == agent["metadata"]["uid"]
                    for owner in pod["metadata"].get("ownerReferences", [])
                )
            )
            for pod in pods:
                node = self._kubectl.get("node", pod["spec"]["nodeName"])
                directories = inspection.runtime_paths(node, timeout)
                container, host_conf, conf, binaries = _cni_mounts(pod, directories)
                arguments = (*container.get("command", []), *container.get("args", []))
                daemon_config = ""
                for index, argument in enumerate(arguments):
                    if argument.startswith("--config="):
                        daemon_config = argument.split("=", 1)[1]
                    elif argument == "--config":
                        daemon_config = arguments[index + 1]
                identity = f"{namespace}/{pod['metadata']['name']}"
                documents = self._read_node_configuration(
                    pod,
                    container,
                    conf,
                    binaries,
                    daemon_config,
                    inspection,
                    timeout,
                )
                ipam = conf + "/whereabouts.d/whereabouts.conf"
                if not isinstance(documents[ipam], dict):
                    raise DeploymentError(
                        f"invalid Whereabouts shared configuration on {identity}"
                    )
                networks = sorted(
                    (name, value)
                    for name, value in documents.items()
                    if name.startswith(conf + "/")
                    and "/" not in name[len(conf) + 1 :]
                    and isinstance(value, dict)
                    and ("type" in value or "plugins" in value)
                )
                active = networks[0][1] if networks else {}
                policies = [
                    entry
                    for entry in active.get("plugins", [active])
                    if entry.get("type") in {"multus", "multus-shim"}
                ]
                if not policies:
                    raise DeploymentError(
                        f"the active node CNI configuration is not Multus on {identity}"
                    )
                if any(policy.get("type") == "multus-shim" for policy in policies):
                    if not daemon_config:
                        raise DeploymentError(
                            f"cannot inspect the Multus daemon namespace policy on {identity}"
                        )
                    daemon = documents[daemon_config]
                    if daemon.get("multusConfigFile", "auto") not in {"", "auto"}:
                        raise DeploymentError(
                            f"Multus uses a separate namespace-policy file on {identity}; retain its existing CNI owner"
                        )
                    policies.append(daemon)
                for policy in policies:
                    if not policy.get("namespaceIsolation"):
                        continue
                    allowed = {
                        name.strip()
                        for name in (policy.get("globalNamespaces") or "default").split(
                            ","
                        )
                    }
                    if self._helm.config.namespace not in allowed:
                        raise DeploymentError(
                            f"Multus on {identity} does not permit shared NADs from "
                            f"namespace {self._helm.config.namespace}; retain its existing namespace policy"
                        )
                paths.add(host_conf + "/whereabouts.d/whereabouts.conf")
        if len(paths) != 1:
            raise DeploymentError(
                "Multus nodes must expose one common installed Whereabouts configuration path; "
                + (
                    ", ".join(sorted(paths))
                    if paths
                    else "no running node CNI installation was observed"
                )
            )
        return paths.pop()

    def _read_node_configuration(
        self,
        pod: dict[str, Any],
        container: dict[str, Any],
        conf: str,
        binaries: list[str],
        daemon: str,
        inspection: NodeInspection,
        timeout: str,
    ) -> dict[str, Any]:
        """Read node CNI files independently of the agent image, including distroless installations."""
        volumes = {value["name"]: value for value in pod["spec"]["volumes"]}
        mounts = container.get("volumeMounts", [])
        host_paths = {
            volumes[mount["name"]]["hostPath"]["path"]: mount["mountPath"]
            for mount in mounts
            if mount["mountPath"].rstrip("/") in {conf, *binaries}
        }
        daemon_file = ""
        daemon_document = None
        if daemon:
            for mount in sorted(
                mounts, key=lambda value: len(value["mountPath"]), reverse=True
            ):
                try:
                    relative = PurePosixPath(daemon).relative_to(mount["mountPath"])
                except ValueError:
                    continue
                source = volumes[mount["name"]]
                relative = PurePosixPath(mount.get("subPath", "")) / relative
                if "hostPath" in source:
                    host_paths[
                        str(PurePosixPath(source["hostPath"]["path"]) / relative)
                    ] = daemon
                    daemon_file = daemon
                elif "configMap" in source:
                    config = source["configMap"]
                    key = str(relative)
                    if "items" in config:
                        key = next(
                            item["key"]
                            for item in config["items"]
                            if item["path"] == str(relative)
                        )
                    value = self._kubectl.get(
                        "configmap", config["name"], pod["metadata"]["namespace"]
                    )
                    daemon_document = json.loads(value["data"][key])
                else:
                    raise DeploymentError(
                        f"cannot inspect Multus daemon configuration from volume {mount['name']}"
                    )
                break
            else:
                raise DeploymentError(
                    f"Multus daemon configuration {daemon} has no observable node or ConfigMap mount"
                )
        documents = inspection.run(
            pod["spec"]["nodeName"],
            Path(__file__).with_name("cni_files.py").read_text(),
            timeout,
            arguments=(conf, json.dumps(binaries), daemon_file),
            host_paths=host_paths,
        )
        if daemon_document is not None:
            documents[daemon] = daemon_document
        return documents

    def finish_uninstall(self, timeout: str) -> tuple[str, str]:
        """Retire only our unused node agents, preserving shared APIs and external owners."""
        helm, kubectl, release = self._helm, self._kubectl, self._release
        if not helm.release_exists(release):
            return "Preserve", "no CLI-managed Multus release"
        if not helm.is_cleanup_managed(release):
            return "Preserve", f"{release.display_name} is externally managed"
        if NAD_RESOURCE in kubectl.api_resource_names("k8s.cni.cncf.io"):
            networks = kubectl.list_all_resources((NAD_RESOURCE,))
            if networks:
                names = ", ".join(
                    f"{value['metadata']['namespace']}/{value['metadata']['name']}"
                    for value in networks
                )
                return "Preserve", f"NetworkAttachmentDefinitions remain: {names}"
        # A Pod can retain a network selection after its NAD has been deleted.
        pods = tuple(
            value
            for value in kubectl.list_all_resources(("pods",))
            if value["metadata"].get("annotations", {}).get(_NETWORK_ANNOTATION)
        )
        if pods:
            names = ", ".join(
                f"{value['metadata']['namespace']}/{value['metadata']['name']}"
                for value in pods
            )
            return "Preserve", f"Pods retain secondary-network annotations: {names}"
        helm.uninstall(release, timeout)
        return "Removed", release.display_name

    def _ensure_apis(self, timeout: str, *, install: bool) -> None:
        """Require shared APIs, preparing missing definitions only for CLI-owned agents."""
        available = {
            name
            for group in ("k8s.cni.cncf.io", "whereabouts.cni.cncf.io")
            for name in self._kubectl.api_resource_names(group)
        }
        missing = tuple((name, url) for name, url in _CRDS if name not in available)
        if missing and not install:
            raise DeploymentError(
                "the existing Multus installation must provide the secondary-network APIs: "
                + ", ".join(name for name, _ in missing)
                + "; prepare Whereabouts through its existing CNI owner"
            )
        for _, url in missing:
            selected = select_github_download(
                url, os.environ.get("FORETOKEN_GITHUB_MIRROR")
            )
            self._kubectl.run(
                ["apply", "--server-side", "--filename", selected],
                timeout=timeout_seconds(timeout),
            )
        if missing:
            self._kubectl.wait_for_crds(tuple(name for name, _ in missing), timeout)


def _cni_mounts(
    pod: dict[str, Any],
    directories: dict[str, Any],
) -> tuple[dict[str, Any], str, str, list[str]]:
    """Locate effective runtime CNI directories in the agent's actual host mounts."""
    spec = pod["spec"]
    volumes = {
        volume["name"]: volume["hostPath"]["path"].rstrip("/")
        for volume in spec.get("volumes", [])
        if "hostPath" in volume
    }
    for container in spec["containers"]:
        mounts = [
            (volumes[mount["name"]], mount["mountPath"].rstrip("/"), mount["name"])
            for mount in container.get("volumeMounts", [])
            if mount["name"] in volumes
            and not mount.get("subPath")
            and not mount.get("subPathExpr")
        ]
        configurations = [
            (host, path)
            for host, path, _ in mounts
            if host == directories["confDir"].rstrip("/")
        ]
        binaries = [
            path
            for host, path, _ in mounts
            if host in {directory.rstrip("/") for directory in directories["binDirs"]}
        ]
        if len(configurations) == 1 and binaries:
            host, path = configurations[0]
            return container, host, path, binaries
    raise DeploymentError(
        f"cannot locate observable host CNI configuration and binary mounts on "
        f"{pod['metadata']['namespace']}/{pod['metadata']['name']}; retain its existing CNI owner"
    )


def _is_multus(value: dict[str, Any]) -> bool:
    """Identify upstream Multus node agents, including mirrored and renamed deployments."""
    metadata = value["metadata"]
    template = value["spec"]["template"]
    identities = {"multus", "multus-cni", "rke2-multus"}
    for labels in (
        metadata.get("labels", {}),
        template.get("metadata", {}).get("labels", {}),
    ):
        if any(
            labels.get(key) in identities
            for key in ("app", "k8s-app", "app.kubernetes.io/name")
        ):
            return True
    pod = template["spec"]
    for container in (*pod.get("initContainers", []), *pod["containers"]):
        image = (
            container.get("image", "")
            .rsplit("/", 1)[-1]
            .split("@", 1)[0]
            .split(":", 1)[0]
        )
        if image in {
            "multus-cni",
            "multus-cni-thick",
            "hardened-multus-cni",
            "hardened-multus-thick",
        }:
            return True
        if any(
            argument.rsplit("/", 1)[-1]
            in {"thin_entrypoint", "multus", "multus-daemon"}
            for argument in container.get("command", [])
        ):
            return True
    return False


def _cni_defaults(
    directories: tuple[dict[str, Any], ...], values: dict[str, Any]
) -> dict[str, Any]:
    """Project effective runtime paths into the managed Multus and Whereabouts installation."""
    paths = values.get("config", {}).get("cni_conf", {})
    layouts = set()
    for directory in directories:
        conf_dir = paths.get("confDir", directory["confDir"])
        bin_dir = paths.get("binDir", directory["binDirs"][0])
        if conf_dir != directory["confDir"] or bin_dir not in directory["binDirs"]:
            raise DeploymentError(
                "stored Multus CNI directories do not match the running container runtime"
            )
        layouts.add((conf_dir, bin_dir))
    if len(layouts) != 1:
        raise DeploymentError(
            "the managed Multus release requires common runtime CNI directories; "
            "reuse an existing CNI installation for nodes with different layouts"
        )
    conf_dir, bin_dir = layouts.pop()
    return {
        "config.cni_conf.confDir": conf_dir,
        "config.cni_conf.binDir": bin_dir,
        "config.cni_conf.kubeconfig": f"{conf_dir}/multus.d/multus.kubeconfig",
        "config.cni_conf.multusAutoconfigDir": conf_dir,
        "config.daemon_conf.cniConfigDir": "/host" + conf_dir,
        "config.daemon_conf.multusAutoconfigDir": "/host" + conf_dir,
        "rke2-whereabouts.cniConf.confDir": conf_dir,
        "rke2-whereabouts.cniConf.binDir": bin_dir,
    }
