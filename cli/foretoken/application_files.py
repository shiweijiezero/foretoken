# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own short-lived file publishers and address the platform's persistent origin."""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from importlib.resources import files
from typing import Any
from urllib.parse import unquote, urlsplit

from foretoken.cluster_build import ClusterBuilder
from foretoken.kubernetes import Kubectl, timeout_seconds
from foretoken.manifest import DeploymentError, ResourceRef

SOURCE_REVISION = "inference.foretoken.io/source-revision"
_BINDING_LABEL = "inference.foretoken.io/application-publisher-binding"


def application_references(objects: Iterable[dict[str, Any]]) -> set[str]:
    """Read retained application identities from service state and workload projections."""
    references: set[str] = set()
    for obj in objects:
        spec = obj.get("spec", {})
        template = spec.get("template", {})
        metadata = obj["metadata"]
        template_metadata = template.get("metadata", {})
        references.update(
            selection["applicationURL"]
            for selection in obj.get("status", {}).get("poolApplications", {}).values()
            if selection.get("applicationURL")
        )
        references.update(
            filter(
                None,
                (
                    metadata.get("annotations", {}).get(SOURCE_REVISION),
                    template_metadata.get("annotations", {}).get(SOURCE_REVISION),
                    metadata.get("annotations", {}).get(
                        "inference.foretoken.io/application-url"
                    ),
                    template_metadata.get("annotations", {}).get(
                        "inference.foretoken.io/application-url"
                    ),
                    template.get("sourceRevision"),
                    template.get("application", {}).get("applicationURL"),
                    spec.get("runtime", {}).get("applicationURL"),
                    obj.get("status", {}).get("application", {}).get("applicationURL"),
                    spec.get("runtime", {}).get("sourceRevision"),
                    obj.get("status", {}).get("plan", {}).get("workerApplicationURL"),
                ),
            )
        )
        # Platform defaults remain consumers even before a model service uses them.
        for container in template.get("spec", spec).get("containers", []):
            for argument in container.get("args", []):
                for prefix in (
                    "--frontend-application-url=",
                    "--model-server-application-url=",
                    "--video-worker-application-url=",
                ):
                    if argument.startswith(prefix):
                        references.add(argument.removeprefix(prefix))
    return references


def remove_application_jobs(kubectl: Kubectl, binding: str, timeout: str) -> None:
    """Stop abandoned writers before their workstation reuses its compiler output."""
    kubectl.run(
        [
            "delete",
            "jobs",
            "--all-namespaces",
            "--selector",
            f"foretoken.io/application-files=publisher,{_BINDING_LABEL}={binding}",
            "--ignore-not-found",
            "--cascade=foreground",
            "--wait=true",
            "--timeout=" + timeout,
        ]
    )


class ApplicationFiles:
    """Resolve the installed file origin without taking ownership of model storage."""

    def __init__(self, kubectl: Kubectl, namespace: str) -> None:
        self.kubectl = kubectl
        self.namespace = namespace
        configurations = kubectl.list_resources(
            ("configmaps",),
            namespace,
            label_selector="foretoken.io/application-files=configuration",
        )
        if len(configurations) != 1:
            raise DeploymentError(
                "application file storage is unavailable; update the source installation"
            )
        configuration = configurations[0]
        self.name = configuration["metadata"]["name"]
        data = configuration["data"]
        self.claim = data["claim"]
        self.endpoint = data["endpoint"]
        self.client_image = data["clientImage"]
        self.mount = data["storageMount"]

    def prepare(self, timeout: str) -> None:
        """Wait for the file origin and select its volume's current publisher node."""
        kubectl, namespace = self.kubectl, self.namespace
        kubectl.rollout_status(ResourceRef("Deployment", self.name, namespace), timeout)
        self.claim_uid = kubectl.get("pvc", self.claim, namespace)["metadata"]["uid"]
        deployment = kubectl.get("deployment", self.name, namespace)
        self.pull_secrets = tuple(
            secret["name"]
            for secret in deployment["spec"]["template"]["spec"].get(
                "imagePullSecrets", []
            )
        )
        selector = ",".join(
            f"{key}={value}"
            for key, value in deployment["spec"]["selector"]["matchLabels"].items()
        )
        pods = kubectl.list_resources(("pods",), namespace, label_selector=selector)
        ready = [
            pod
            for pod in pods
            if not pod["metadata"].get("deletionTimestamp")
            and any(
                condition["type"] == "Ready" and condition["status"] == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
        ]
        if len(ready) != 1:
            raise DeploymentError(
                "application file server has no single ready publisher node"
            )
        self.node = ready[0]["spec"]["nodeName"]

    def reference(self, component: str, revision: str) -> str:
        """Return the immutable HTTP directory selected by an application consumer."""
        return f"{self.endpoint}/{component}/{revision}"

    def references(self, history: set[str] | None) -> set[str] | None:
        """Retain Helm history, service intent and all current or terminating file consumers."""
        if history is None:
            return None
        references = history.copy()
        available = self.kubectl.api_resource_names("inference.foretoken.io")
        kinds = tuple(
            kind
            for kind in (
                "modelservices",
                "frontendservices",
                "modelpools",
                "modelgroups",
                "videotasks",
            )
            if kind + ".inference.foretoken.io" in available
        )
        objects = list(self.kubectl.list_all_resources(kinds)) if kinds else []
        for label in (
            "inference.foretoken.io/model-group",
            "inference.foretoken.io/frontend-service",
            "inference.foretoken.io/model-preparation-group",
            "foretoken.io/application-files=consumer",
        ):
            objects.extend(
                self.kubectl.list_all_resources(
                    ("pods", "jobs", "replicasets", "deployments"),
                    label_selector=label,
                )
            )
        references.update(application_references(objects))
        return references

    def publish(
        self,
        builder: ClusterBuilder,
        source: str,
        component: str,
        revision: str,
        previous: str,
        references: set[str] | None,
        *,
        timeout: str,
    ) -> None:
        """Publish a compiler export while the source caller holds its binding lock."""
        command = [
            "python",
            "-c",
            files("foretoken").joinpath("application_publish.py").read_text(),
            source,
            f"{self.mount}/{component}/{revision}",
            builder.binding,
            f"{self.mount}/{component}/{previous}" if previous else "",
            json.dumps(self._retained_versions(references)),
        ]
        with self._publisher(command, builder.binding, timeout, builder=builder) as job:
            self._wait(job, timeout)

    @contextmanager
    def import_release(
        self,
        source: str,
        revision: str,
        references: Callable[[], set[str] | None],
        previous: Callable[[], dict[str, str]],
        *,
        timeout: str,
        credentials_secret: str = "",
    ) -> Iterator[None]:
        """Serialize release import and GC through the caller's Helm selection commit.

        The input ConfigMap is created only after unique Job ownership, so a Pending
        Pod cannot collect files using a snapshot taken before another installer commits.
        """
        binding = "release-" + uuid.uuid4().hex
        input_name = "foretoken-publish-input-" + uuid.uuid4().hex[:12]
        command = [
            "python",
            "-c",
            files("foretoken").joinpath("application_publish.py").read_text(),
            "--release",
            source,
            self.mount,
            revision,
            binding,
            "/selection/input.json",
        ]
        with self._publisher(
            command,
            binding,
            timeout,
            name="foretoken-release-" + self.claim_uid,
            input_name=input_name,
            credentials_secret=credentials_secret,
        ) as job:
            selection = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": input_name,
                    "namespace": self.namespace,
                    "ownerReferences": [
                        {
                            "apiVersion": "batch/v1",
                            "kind": "Job",
                            "name": job["name"],
                            "uid": job["uid"],
                        }
                    ],
                },
                "immutable": True,
                "data": {
                    "input.json": json.dumps(
                        {
                            "keep": self._retained_versions(references()),
                            "previous": previous(),
                        }
                    )
                },
            }
            self.kubectl.run(["create", "-f", "-"], input_text=json.dumps(selection))
            self._wait(job, timeout)
            # Release the RWO mount without releasing this installer's operation name.
            for pod in self.kubectl.list_resources(
                ("pods",),
                self.namespace,
                label_selector="batch.kubernetes.io/controller-uid=" + job["uid"],
            ):
                if any(
                    owner.get("kind") == "Job"
                    and owner.get("uid") == job["uid"]
                    and owner.get("controller") is True
                    for owner in pod["metadata"].get("ownerReferences", [])
                ):
                    self._delete_owned("pods", pod["metadata"], timeout)
            current = self.kubectl.get_if_exists("job", job["name"], self.namespace)
            if current is None or current["metadata"]["uid"] != job["uid"]:
                raise DeploymentError(
                    "application release operation expired before Helm selection"
                )
            yield

    @staticmethod
    def _retained_versions(references: set[str] | None) -> list[str] | None:
        """Translate retained URLs or source revisions into immutable directory names."""
        return (
            None
            if references is None
            else sorted(
                {
                    unquote(urlsplit(reference).path).rstrip("/").rsplit("/", 1)[-1]
                    for reference in references
                    if reference
                }
            )
        )

    @contextmanager
    def _publisher(
        self,
        command: list[str],
        binding: str,
        timeout: str,
        *,
        builder: ClusterBuilder | None = None,
        name: str = "",
        input_name: str = "",
        credentials_secret: str = "",
    ) -> Iterator[dict[str, Any]]:
        """Own one native publisher Job, waiting for an occupied release operation name."""
        name = name or "foretoken-publish-" + uuid.uuid4().hex[:12]
        seconds = math.ceil(timeout_seconds(timeout))
        labels = {
            "foretoken.io/application-files": "publisher",
            _BINDING_LABEL: binding,
        }
        mounts = [{"name": "applications", "mountPath": self.mount}]
        volumes = [
            {"name": "applications", "persistentVolumeClaim": {"claimName": self.claim}}
        ]
        if builder is not None:
            mounts.insert(
                0, {"name": "compiler", "mountPath": builder.mount, "readOnly": True}
            )
            volumes.insert(
                0,
                {
                    "name": "compiler",
                    "persistentVolumeClaim": {
                        "claimName": builder.claim,
                        "readOnly": True,
                    },
                },
            )
        if input_name:
            mounts.extend(
                [
                    {"name": "temporary", "mountPath": "/tmp"},
                    {"name": "selection", "mountPath": "/selection", "readOnly": True},
                ]
            )
            volumes.extend(
                [
                    {"name": "temporary", "emptyDir": {}},
                    {"name": "selection", "configMap": {"name": input_name}},
                ]
            )
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": labels,
                "ownerReferences": [
                    {
                        "apiVersion": "v1",
                        "kind": "PersistentVolumeClaim",
                        "name": self.claim,
                        "uid": self.claim_uid,
                    }
                ],
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": seconds,
                "ttlSecondsAfterFinished": seconds,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "imagePullSecrets": [
                            {"name": secret} for secret in self.pull_secrets
                        ],
                        "affinity": {
                            "nodeAffinity": {
                                "requiredDuringSchedulingIgnoredDuringExecution": {
                                    "nodeSelectorTerms": [
                                        {
                                            "matchFields": [
                                                {
                                                    "key": "metadata.name",
                                                    "operator": "In",
                                                    "values": [self.node],
                                                }
                                            ]
                                        }
                                    ],
                                }
                            }
                        },
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "runAsGroup": 1000,
                            "fsGroup": 1000,
                            "fsGroupChangePolicy": "OnRootMismatch",
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "publish",
                                "image": self.client_image,
                                "command": command,
                                "terminationMessagePolicy": "FallbackToLogsOnError",
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": mounts,
                            }
                        ],
                        "volumes": volumes,
                    },
                },
            },
        }
        if credentials_secret:
            job["spec"]["template"]["spec"]["containers"][0]["env"] = [
                {
                    "name": "FORETOKEN_RELEASE_AUTHORIZATION",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": credentials_secret,
                            "key": "authorization",
                        }
                    },
                }
            ]
        deadline = time.monotonic() + seconds
        while True:
            try:
                created = self.kubectl.run(
                    ["create", "-f", "-", "-o", "json"], input_text=json.dumps(job)
                )
                break
            except DeploymentError as exc:
                # Only an occupied operation name is recoverable; never stop its owner.
                if not input_name or "AlreadyExists" not in str(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise DeploymentError(
                        f"application release operation {self.namespace}/{name} is occupied after {timeout}"
                    ) from None
                time.sleep(1)
        metadata = json.loads(created.stdout)["metadata"]
        try:
            yield metadata
        finally:
            self._delete_owned("jobs", metadata, timeout)

    def _delete_owned(
        self, resource: str, metadata: dict[str, Any], timeout: str
    ) -> None:
        """Delete and await only the created UID, even after TTL cleanup and name reuse."""
        name, uid = metadata["name"], metadata["uid"]
        current = self.kubectl.get_if_exists(resource, name, self.namespace)
        if current is None or current["metadata"]["uid"] != uid:
            return
        prefix = "/apis/batch/v1" if resource == "jobs" else "/api/v1"
        try:
            self.kubectl.run(
                [
                    "delete",
                    "--raw",
                    f"{prefix}/namespaces/{self.namespace}/{resource}/{name}",
                    "-f",
                    "-",
                ],
                input_text=json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "preconditions": {"uid": uid},
                        "propagationPolicy": "Foreground",
                    }
                ),
            )
        except DeploymentError:
            current = self.kubectl.get_if_exists(resource, name, self.namespace)
            if current is not None and current["metadata"]["uid"] == uid:
                raise
            return
        deadline = time.monotonic() + timeout_seconds(timeout)
        while True:
            current = self.kubectl.get_if_exists(resource, name, self.namespace)
            if current is None or current["metadata"]["uid"] != uid:
                return
            if time.monotonic() >= deadline:
                raise DeploymentError(
                    f"application publisher {resource}/{name} did not terminate within {timeout}"
                )
            time.sleep(1)

    def _wait(self, job: dict[str, Any], timeout: str) -> None:
        """Wait for the owned Job's publication and surface native failure diagnostics."""
        name = job["name"]
        deadline = time.monotonic() + timeout_seconds(timeout)
        while True:
            current = self.kubectl.get_if_exists("job", name, self.namespace)
            if current is None or current["metadata"]["uid"] != job["uid"]:
                raise DeploymentError(
                    f"application publication {self.namespace}/{name} lost Job ownership"
                )
            conditions = current.get("status", {}).get("conditions", [])
            if any(
                c["type"] == "Complete" and c["status"] == "True" for c in conditions
            ):
                return
            failed = next(
                (
                    c
                    for c in conditions
                    if c["type"] == "Failed" and c["status"] == "True"
                ),
                None,
            )
            if failed is not None:
                messages = [failed.get("message", failed.get("reason", "Job failed"))]
                for pod in self.kubectl.list_resources(
                    ("pods",),
                    self.namespace,
                    label_selector="batch.kubernetes.io/controller-uid=" + job["uid"],
                ):
                    for container in pod.get("status", {}).get("containerStatuses", []):
                        if (
                            message := container.get("state", {})
                            .get("terminated", {})
                            .get("message")
                        ):
                            messages.append(message)
                raise DeploymentError(
                    f"application publication {self.namespace}/{name} failed: "
                    + "\n".join(messages)
                )
            if time.monotonic() >= deadline:
                raise DeploymentError(
                    f"application publication {self.namespace}/{name} did not finish within {timeout}"
                )
            time.sleep(1)
