# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own node-local compiler volumes and their dedicated upstream provisioner."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from foretoken.build.cluster.images import (
    SOURCE_BUILD_BINDING_LABEL,
    SOURCE_BUILD_POD_LABEL,
    image_tools_image,
    node_containerd_socket,
)
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, resource_ref, timeout_seconds

SOURCE_BUILD_CACHE_LABEL = "inference.foretoken.io/source-build-cache"
SOURCE_BUILD_UID = 65531
SOURCE_BUILD_STORAGE_CLASS = "foretoken-source-build"
SOURCE_BUILD_PROVISIONER = "inference.foretoken.io/source-build"
SOURCE_BUILD_PROVISIONER_IMAGE = "docker.io/rancher/local-path-provisioner:v0.0.37"

_OWNER_LABEL = "inference.foretoken.io/source-build-storage"
_NODE_DIRECTORY = "/var/lib/foretoken/source-build"
_NAMESPACED_KINDS = (
    "deployments",
    "configmaps",
    "serviceaccounts",
    "roles",
    "rolebindings",
)
_CLUSTER_KINDS = ("storageclasses", "clusterroles", "clusterrolebindings")


def build_configuration(
    root: Path, values: Sequence[dict[str, Any]], image_registry: str
) -> dict[str, str]:
    """Resolve compiler storage and image choices from the chart's authoritative defaults."""
    defaults = yaml.safe_load(
        (root / "deploy/charts/foretoken/values.yaml").read_text()
    )
    configuration = dict(defaults["development"]["build"])
    explicit_image = False
    for value in values:
        overrides = (value.get("development") or {}).get("build") or {}
        explicit_image = explicit_image or "image" in overrides
        configuration.update(overrides)
    if image_registry and not explicit_image:
        configuration["image"] = (
            image_registry.rstrip("/") + "/" + configuration["image"].split("/", 1)[1]
        )
    return configuration


def prepare_build_storage(
    kubectl: Kubectl,
    namespace: str,
    arguments: dict[str, str],
    registry_mirrors: dict[str, list[str]],
    pull_secrets: tuple[str, ...],
    timeout: str,
) -> None:
    """Prepare node-local execution storage for installation, preserving artifact storage intent."""
    automatic = registry_mirrors.get("docker.io", ["docker.io"])[0]
    registry = arguments.get("BASE_IMAGE_REGISTRY", automatic).rstrip("/")
    image = registry + "/" + SOURCE_BUILD_PROVISIONER_IMAGE.split("/", 1)[1]
    ensure_build_storage(
        kubectl, namespace, image_tools_image(arguments, registry_mirrors),
        image, pull_secrets, timeout,
    )


def ensure_build_storage(
    kubectl: Kubectl,
    namespace: str,
    tools_image: str,
    provisioner_image: str,
    pull_secrets: tuple[str, ...],
    timeout: str,
) -> str:
    """Prepare shared source storage before builds and return its non-default StorageClass.

    The source installation owns this dependency across checkout bindings and
    supplies its selected images and namespace-local pull secrets.
    """
    resources = _resources(namespace, tools_image, provisioner_image, pull_secrets, _node_paths(kubectl))
    for document in resources:
        ref = resource_ref(document)
        existing = kubectl.get_if_exists(ref.kind, ref.name, ref.namespace)
        if (
            existing is not None
            and existing["metadata"].get("labels", {}).get(_OWNER_LABEL) != namespace
        ):
            raise DeploymentError(
                f"cannot prepare source storage: {ref.display_name} already exists outside this installation"
            )
    if not kubectl.exists("namespace", namespace):
        kubectl.apply(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": namespace},
                }
            )
        )
    deployment = next(document for document in resources if document["kind"] == "Deployment")
    kubectl.apply(yaml.safe_dump_all((document for document in resources if document is not deployment), sort_keys=False))
    configuration = kubectl.get("configmap", SOURCE_BUILD_STORAGE_CLASS, namespace)
    # Start with the current path selection before any new claim can be provisioned.
    deployment["spec"]["template"]["metadata"]["annotations"] = {
        "inference.foretoken.io/source-build-config-revision": configuration["metadata"]["resourceVersion"],
    }
    kubectl.apply(yaml.safe_dump(deployment, sort_keys=False))
    kubectl.rollout_status(resource_ref(deployment), timeout)
    return SOURCE_BUILD_STORAGE_CLASS


def ensure_build_volumes(
    kubectl: Kubectl,
    namespace: str,
    name: str,
    configuration: dict[str, str],
    *,
    binding: str,
    timeout: str,
    record_identity: Callable[[dict[str, Any]], None],
    recorded_caches: Sequence[dict[str, Any]] = (),
    owner: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Prepare artifact and optional local execution PVCs for source build callers.

    The source state owner records each UID before another PVC is created, so a
    failed pair remains recoverable. Default local storage uses a single volume;
    explicitly selected artifact storage receives a separate local execution PVC.
    """
    artifact = _ensure_build_cache(
        kubectl, namespace, name, configuration,
        binding=binding, timeout=timeout, recorded_caches=recorded_caches, owner=owner,
    )
    if artifact is not None:
        record_identity(artifact)
    # An omitted override preserves an existing volume's class, including NFS.
    current = kubectl.get("pvc", name, namespace)
    if current["spec"].get("storageClassName", "") in ("", SOURCE_BUILD_STORAGE_CLASS):
        return artifact, None
    if artifact is None:
        raise DeploymentError(
            f"cannot prepare execution storage: artifact PVC {namespace}/{name} has no proven source binding"
        )
    if artifact.get("legacy"):
        metadata = current["metadata"]
        labels = metadata.get("labels", {})
        if metadata["uid"] != artifact["uid"] or labels.get(SOURCE_BUILD_CACHE_LABEL) != "true" or labels.get(SOURCE_BUILD_BINDING_LABEL) not in (None, binding):
            raise DeploymentError(f"artifact PVC {namespace}/{name} changed during ownership transfer")
        updated = json.loads(kubectl.run([
            "patch", "pvc", name, "-n", namespace, "--type=json", "-o", "json", "-p", json.dumps([
                {"op": "test", "path": "/metadata/uid", "value": artifact["uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
                {"op": "add", "path": "/metadata/labels/" + SOURCE_BUILD_BINDING_LABEL.replace("/", "~1"), "value": binding},
            ]),
        ]).stdout)["metadata"]
        artifact = {key: updated[key] for key in ("namespace", "name", "uid")}
        artifact["binding"] = updated["labels"][SOURCE_BUILD_BINDING_LABEL]
        record_identity(artifact)
    execution_name = "foretoken-execution-" + artifact["uid"][:20]
    execution = _ensure_build_cache(
        kubectl, namespace, execution_name,
        {**configuration, "storageClassName": SOURCE_BUILD_STORAGE_CLASS},
        binding=binding, timeout=timeout, recorded_caches=recorded_caches, owner=owner,
    )
    if execution is None:
        raise DeploymentError(
            f"execution PVC {namespace}/{execution_name} has no proven source binding"
        )
    record_identity(execution)
    return artifact, execution


def managed_build_storage_resources(
    kubectl: Kubectl, namespace: str
) -> tuple[dict[str, Any], ...]:
    """Discover owned storage resources, including a bootstrap without workstation state."""
    selector = f"{_OWNER_LABEL}={namespace}"
    return (
        *kubectl.list_resources(_NAMESPACED_KINDS, namespace, label_selector=selector),
        *kubectl.list_cluster_resources(_CLUSTER_KINDS, label_selector=selector),
    )


def remove_build_storage(
    kubectl: Kubectl, namespace: str, timeout: str
) -> tuple[str, str]:
    """Remove owned provisioning resources after all compiler volumes finish teardown.

    Uninstall calls this after releasing builders and their PVCs. Live claims or
    retained volumes preserve the provisioner; a failed teardown leaves it running.
    The platform namespace and unrelated storage resources are never removed.
    """
    selector = f"{_OWNER_LABEL}={namespace}"
    owned = managed_build_storage_resources(kubectl, namespace)
    if not owned:
        return "Preserve", "no managed source storage provisioner"
    deadline = time.monotonic() + timeout_seconds(timeout)
    # Keep the controller and helper permissions until Delete-policy PVs disappear.
    while True:
        classes = {
            storage_class["metadata"]["name"]
            for storage_class in kubectl.list_cluster_resources(("storageclasses",))
            if storage_class.get("provisioner") == SOURCE_BUILD_PROVISIONER
        }
        classes.add(SOURCE_BUILD_STORAGE_CLASS)
        claims = [
            claim
            for claim in kubectl.list_all_resources(("persistentvolumeclaims",))
            if claim["spec"].get("storageClassName") in classes
        ]
        active = [
            claim for claim in claims if not claim["metadata"].get("deletionTimestamp")
        ]
        if active:
            return "Preserve", "source storage still has PVC consumers"
        volumes = [
            volume
            for volume in kubectl.list_cluster_resources(("persistentvolumes",))
            if volume["spec"].get("storageClassName") in classes
            or volume["metadata"]
            .get("annotations", {})
            .get("pv.kubernetes.io/provisioned-by")
            == SOURCE_BUILD_PROVISIONER
        ]
        if any(
            volume["spec"].get("persistentVolumeReclaimPolicy") != "Delete"
            for volume in volumes
        ):
            return "Preserve", "retained source volumes still need the provisioner"
        if not claims and not volumes:
            break
        if time.monotonic() >= deadline:
            raise DeploymentError(
                "source storage volume teardown did not finish within " + timeout
            )
        time.sleep(1)
    if classes - {SOURCE_BUILD_STORAGE_CLASS}:
        return "Preserve", "another StorageClass uses the source provisioner"
    # Label selection is evaluated by the server, so an unowned replacement is untouched.
    for kinds, scope in (
        (_CLUSTER_KINDS, []),
        (_NAMESPACED_KINDS, ["--namespace", namespace]),
    ):
        kubectl.run(
            [
                "delete",
                ",".join(kinds),
                *scope,
                "--selector",
                selector,
                "--ignore-not-found",
                "--wait=true",
                "--timeout=" + timeout,
            ]
        )
    return "Removed", SOURCE_BUILD_STORAGE_CLASS


def _node_paths(kubectl: Kubectl) -> list[dict[str, Any]]:
    """Reuse upstream local-volume roots, including persistent mounts inside kind and k3d nodes."""
    classes = sorted(
        kubectl.list_cluster_resources(("storageclasses",)),
        key=lambda item: (
            item["metadata"].get("annotations", {}).get("storageclass.kubernetes.io/is-default-class") != "true",
            item["metadata"]["name"],
        ),
    )
    deployments = kubectl.list_all_resources(("deployments",))
    for storage_class in classes:
        if storage_class["provisioner"] == SOURCE_BUILD_PROVISIONER:
            continue
        for deployment in deployments:
            spec = deployment["spec"]["template"]["spec"]
            for container in spec["containers"]:
                command = [*container.get("command", []), *container.get("args", [])]
                if not any(PurePosixPath(arg).name == "local-path-provisioner" for arg in command):
                    continue
                options = {}
                for index, arg in enumerate(command):
                    if arg.startswith("--"):
                        key, separator, value = arg.partition("=")
                        options[key] = value if separator else command[index + 1] if index + 1 < len(command) else ""
                environment = {item["name"]: item["value"] for item in container.get("env", []) if "value" in item}
                provisioner = options.get("--provisioner-name", environment.get("PROVISIONER_NAME", "rancher.io/local-path"))
                if storage_class["provisioner"] != provisioner:
                    continue
                config_path = PurePosixPath(options.get("--config", "/etc/config/config.json"))
                volumes = {volume["name"]: volume for volume in spec.get("volumes", [])}
                for mount in container.get("volumeMounts", []):
                    volume = volumes[mount["name"]]
                    if "configMap" not in volume or config_path.parent != PurePosixPath(mount["mountPath"]):
                        continue
                    config = kubectl.get("configmap", volume["configMap"]["name"], deployment["metadata"]["namespace"])
                    key = next((item["key"] for item in volume["configMap"].get("items", []) if item["path"] == config_path.name), config_path.name)
                    configured = json.loads(config["data"][key])
                    selected = configured.get("storageClassConfigs", {}).get(storage_class["metadata"]["name"], configured)
                    if "nodePathMap" in selected and not selected.get("sharedFileSystemPath"):
                        return [
                            {"node": entry["node"], "paths": [str(PurePosixPath(path) / SOURCE_BUILD_STORAGE_CLASS) for path in entry["paths"]]}
                            for entry in selected["nodePathMap"]
                        ]
    return [{"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES", "paths": [_NODE_DIRECTORY]}]


def _resources(
    namespace: str,
    tools_image: str,
    provisioner_image: str,
    pull_secrets: tuple[str, ...],
    node_paths: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Render the dedicated local-path-provisioner v0.0.37 resources and helper configuration."""
    name = SOURCE_BUILD_STORAGE_CLASS
    labels = {
        _OWNER_LABEL: namespace,
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/managed-by": "foretoken",
    }
    helper = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "helper-pod"},
        "spec": {
            "priorityClassName": "system-node-critical",
            "imagePullSecrets": [{"name": secret} for secret in pull_secrets],
            "tolerations": [
                {
                    "key": "node.kubernetes.io/disk-pressure",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                }
            ],
            "containers": [
                {
                    "name": "helper-pod",
                    "image": tools_image,
                    "imagePullPolicy": "IfNotPresent",
                    "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
                }
            ],
        },
    }
    resources: list[dict[str, Any]] = [
        {"apiVersion": "v1", "kind": "ServiceAccount"},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "verbs": [
                        "get",
                        "list",
                        "watch",
                        "create",
                        "patch",
                        "update",
                        "delete",
                    ],
                }
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": [
                        "nodes",
                        "persistentvolumeclaims",
                        "configmaps",
                        "pods",
                        "pods/log",
                    ],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["persistentvolumes"],
                    "verbs": [
                        "get",
                        "list",
                        "watch",
                        "create",
                        "patch",
                        "update",
                        "delete",
                    ],
                },
                {
                    "apiGroups": [""],
                    "resources": ["events"],
                    "verbs": ["create", "patch"],
                },
                {
                    "apiGroups": ["storage.k8s.io"],
                    "resources": ["storageclasses"],
                    "verbs": ["get", "list", "watch"],
                },
            ],
        },
        *[
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": kind + "Binding",
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": kind,
                    "name": name,
                },
                "subjects": [
                    {"kind": "ServiceAccount", "name": name, "namespace": namespace}
                ],
            }
            for kind in ("Role", "ClusterRole")
        ],
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "data": {
                "config.json": json.dumps({"nodePathMap": node_paths}),
                # A local PV still uses the filesystem beneath its configured node path.
                # Reject NFS before creating a compiler directory or changing its ownership.
                "setup": """#!/bin/sh
set -eu
filesystem=$(stat -f -c %t "$(dirname "$VOL_DIR")")
case "$filesystem" in
    6969) printf 'source build storage requires a non-NFS node filesystem: %s\\n' "$VOL_DIR" >&2; exit 1 ;;
esac
mkdir -p "$VOL_DIR"
chown "$VOL_USER:$VOL_GROUP" "$VOL_DIR"
chmod "$VOL_PERM" "$VOL_DIR"
""",
                "teardown": """#!/bin/sh
set -eu
rm -rf "$VOL_DIR"
""",
                "helperPod.yaml": yaml.safe_dump(helper, sort_keys=False),
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {_OWNER_LABEL: namespace}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": name,
                        "imagePullSecrets": [
                            {"name": secret} for secret in pull_secrets
                        ],
                        "containers": [
                            {
                                "name": "provisioner",
                                "image": provisioner_image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "local-path-provisioner",
                                    "start",
                                    "--config",
                                    "/etc/config/config.json",
                                    "--provisioner-name",
                                    SOURCE_BUILD_PROVISIONER,
                                    "--configmap-name",
                                    name,
                                    "--service-account-name",
                                    name,
                                ],
                                "env": [
                                    {
                                        "name": "POD_NAMESPACE",
                                        "valueFrom": {
                                            "fieldRef": {
                                                "fieldPath": "metadata.namespace"
                                            }
                                        },
                                    },
                                    {
                                        "name": "CONFIG_MOUNT_PATH",
                                        "value": "/etc/config/",
                                    },
                                    {"name": "HEALTH_PORT", "value": "8080"},
                                    {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"},
                                ],
                                "ports": [{"name": "health", "containerPort": 8080}],
                                "startupProbe": {
                                    "httpGet": {"path": "/health", "port": "health"},
                                    "initialDelaySeconds": 5,
                                    "periodSeconds": 5,
                                    "failureThreshold": 12,
                                },
                                "livenessProbe": {
                                    "httpGet": {"path": "/health", "port": "health"},
                                    "initialDelaySeconds": 10,
                                    "periodSeconds": 10,
                                },
                                "readinessProbe": {
                                    "httpGet": {"path": "/ready", "port": "health"},
                                    "initialDelaySeconds": 5,
                                    "periodSeconds": 5,
                                },
                                "volumeMounts": [
                                    {
                                        "name": "config",
                                        "mountPath": "/etc/config/",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                        "volumes": [{"name": "config", "configMap": {"name": name}}],
                    },
                },
            },
        },
        {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {
                "annotations": {
                    "defaultVolumeType": "local",
                    "storageclass.kubernetes.io/is-default-class": "false",
                }
            },
            "provisioner": SOURCE_BUILD_PROVISIONER,
            "volumeBindingMode": "WaitForFirstConsumer",
            "reclaimPolicy": "Delete",
            "parameters": {
                "userPattern": str(SOURCE_BUILD_UID),
                "groupPattern": "1000",
                "permPattern": "2775",
            },
        },
    ]
    for document in resources:
        metadata = document.setdefault("metadata", {})
        metadata.update(name=name, labels=labels)
        if document["kind"] not in {
            "ClusterRole",
            "ClusterRoleBinding",
            "StorageClass",
        }:
            metadata["namespace"] = namespace
    return resources


def superseded_build_volume(kubectl: Kubectl, claim: dict[str, Any]) -> dict[str, Any] | None:
    """Find a managed compiler volume whose old node directory is no longer selected.

    The cache lifecycle replaces only Delete-policy volumes provisioned by Foretoken;
    custom storage and published application volumes retain their existing owners.
    """
    if claim["spec"].get("storageClassName") != SOURCE_BUILD_STORAGE_CLASS or not claim["spec"].get("volumeName"):
        return None
    storage_class = kubectl.get_if_exists("storageclass", SOURCE_BUILD_STORAGE_CLASS)
    namespace = (storage_class or {}).get("metadata", {}).get("labels", {}).get(_OWNER_LABEL)
    if not namespace:
        return None
    configuration = kubectl.get_if_exists("configmap", SOURCE_BUILD_STORAGE_CLASS, namespace)
    if configuration is None:
        return None
    volume = kubectl.get("pv", claim["spec"]["volumeName"])
    annotations = volume["metadata"].get("annotations", {})
    if annotations.get("pv.kubernetes.io/provisioned-by") != SOURCE_BUILD_PROVISIONER:
        return None
    node = annotations["local.path.provisioner/selected-node"]
    node_paths = {entry["node"]: entry["paths"] for entry in json.loads(configuration["data"]["config.json"])["nodePathMap"]}
    paths = node_paths.get(node, node_paths.get("DEFAULT_PATH_FOR_NON_LISTED_NODES", []))
    directory = PurePosixPath((volume["spec"].get("local") or volume["spec"]["hostPath"])["path"])
    if not paths or any(directory.is_relative_to(path) for path in paths):
        return None
    if volume["spec"].get("persistentVolumeReclaimPolicy") != "Delete":
        raise DeploymentError(f"compiler volume {volume['metadata']['name']} needs a local-storage update but its reclaim policy is Retain")
    return volume


def _ensure_build_cache(
    kubectl: Kubectl,
    namespace: str,
    name: str,
    configuration: dict[str, str],
    *,
    binding: str,
    timeout: str,
    recorded_caches: Sequence[dict[str, Any]] = (),
    owner: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Prepare this binding's compiler volume and return its proven identity for retirement.

    Superseded managed storage is reclaimed before rebuilding; legacy ownership
    requires a binding-owned builder or its previously recorded claim UID.
    """
    if not kubectl.exists("namespace", namespace):
        kubectl.run(["create", "namespace", namespace])
    storage_class = configuration["storageClassName"] or SOURCE_BUILD_STORAGE_CLASS
    existing = kubectl.get_if_exists("pvc", name, namespace)
    owners = [owner] if owner is not None else []
    if existing is not None:
        if existing["metadata"].get("labels", {}).get(SOURCE_BUILD_CACHE_LABEL) != "true":
            raise DeploymentError(f"{namespace}/{name} is not a Foretoken build cache")
        claim_binding = existing["metadata"].get("labels", {}).get(SOURCE_BUILD_BINDING_LABEL)
        if claim_binding not in {None, binding}:
            raise DeploymentError(f"{namespace}/{name} belongs to another source binding")
        actual_class = existing["spec"].get("storageClassName", "")
        if configuration["storageClassName"] and actual_class != configuration["storageClassName"]:
            raise DeploymentError(
                f"compiler cache {namespace}/{name} uses StorageClass {actual_class!r}; "
                "an existing compiler volume cannot change its storage class"
            )
        storage_class = actual_class
        recorded_legacy = any(
            identity.get("legacy") is True and identity.get("binding") == binding
            and all(identity.get(key) == existing["metadata"][key] for key in ("namespace", "name", "uid"))
            for identity in recorded_caches
        )
        volume = superseded_build_volume(kubectl, existing)
        if volume is not None:
            consumers = [
                pod for pod in kubectl.list_resources(("pods",), namespace)
                if any(item.get("persistentVolumeClaim", {}).get("claimName") == name for item in pod["spec"].get("volumes", []))
            ]
            for pod in consumers:
                labels = pod["metadata"].get("labels", {})
                if labels.get(SOURCE_BUILD_POD_LABEL) != "true" or labels.get(SOURCE_BUILD_BINDING_LABEL) != binding:
                    raise DeploymentError(f"compiler cache {namespace}/{name} is still used by {pod['metadata']['name']}; finish that operation before updating storage")
            if claim_binding is None and not consumers and not recorded_legacy:
                raise DeploymentError(f"cannot replace legacy compiler cache {namespace}/{name}: its source binding is unknown")
            print(f"Replacing compiler cache {namespace}/{name} on the selected local storage; published applications are retained", flush=True)
            owners = existing["metadata"].get("ownerReferences", owners)
            for resource, obj in [*(("pods", pod) for pod in consumers), ("persistentvolumeclaims", existing)]:
                metadata = obj["metadata"]
                kubectl.run(
                    ["delete", "--raw", f"/api/v1/namespaces/{namespace}/{resource}/{metadata['name']}", "-f", "-"],
                    input_text=json.dumps({"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": metadata["uid"]}}),
                )
                kubectl.wait_deleted(resource, metadata, timeout)
            kubectl.wait_deleted("persistentvolume", {**volume["metadata"], "namespace": ""}, timeout)
            existing = None
    if (
        storage_class == SOURCE_BUILD_STORAGE_CLASS
        and (existing is None or existing.get("status", {}).get("phase") != "Bound")
        and not kubectl.exists("storageclass", storage_class)
    ):
        raise DeploymentError(
            "source build storage is not installed; update the source platform "
            "with `foretoken install -e PATH` before deploying"
        )
    if existing is not None:
        spec = {"resources": {"requests": {"storage": configuration["storageSize"]}}}
        if any(existing["spec"].get(key) != value for key, value in spec.items()):
            kubectl.run(
                [
                    "patch",
                    "pvc",
                    name,
                    "-n",
                    namespace,
                    "--type=merge",
                    "-p",
                    json.dumps({"spec": spec}),
                ]
            )
        if claim_binding is None and not recorded_legacy:
            builders = kubectl.list_resources(
                ("pods",), namespace,
                label_selector=f"{SOURCE_BUILD_POD_LABEL}=true,{SOURCE_BUILD_BINDING_LABEL}={binding}",
            )
            if not any(
                volume["name"] == "cache"
                and volume.get("persistentVolumeClaim", {}).get("claimName") == name
                for pod in builders
                for volume in pod["spec"].get("volumes", [])
            ):
                return None
        return {
            "namespace": namespace, "name": name, "uid": existing["metadata"]["uid"],
            "binding": binding,
            **({"legacy": True} if claim_binding is None else {}),
        }
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": configuration["storageSize"]}},
        "storageClassName": storage_class,
    }
    created = kubectl.run(
        ["create", "-f", "-", "-o", "json"],
        input_text=yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {
                        SOURCE_BUILD_CACHE_LABEL: "true",
                        SOURCE_BUILD_BINDING_LABEL: binding,
                    },
                    **({"ownerReferences": owners} if owners else {}),
                },
                "spec": spec,
            }
        ),
    )
    metadata = json.loads(created.stdout)["metadata"]
    return {"namespace": namespace, "name": name, "uid": metadata["uid"], "binding": binding}


def local_build_nodes(
    kubectl: Kubectl, registry: str | None, socket: str = ""
) -> list[tuple[str, str, str]]:
    """Resolve node-local image stores for local and K3s clusters without a registry."""
    if registry:
        return [("", "", "foretoken-compiler-cache")]
    nodes = [
        node
        for node in kubectl.list_resources(("nodes",), "")
        if not node.get("spec", {}).get("unschedulable")
    ]
    if not nodes:
        raise DeploymentError("source builds need a schedulable Kubernetes node")
    if not socket:
        context = kubectl.run(["config", "current-context"]).stdout.strip()
        sockets = {node_containerd_socket(context, node) for node in nodes}
        if "" in sockets or len(sockets) != 1:
            raise DeploymentError(
                "source installation on a remote cluster requires --registry"
            )
        socket = sockets.pop()
    return [
        (
            node["metadata"]["name"],
            socket,
            "foretoken-compiler-cache-" + node["metadata"]["uid"][:8],
        )
        for node in nodes
    ]
