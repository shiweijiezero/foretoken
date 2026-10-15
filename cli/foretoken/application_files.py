# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own short-lived file publishers and address the platform's persistent origin."""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from foretoken.cluster_build import ClusterBuilder
from foretoken.kubernetes import Kubectl, timeout_seconds
from foretoken.manifest import DeploymentError, ResourceRef

SOURCE_REVISION = "inference.foretoken.io/source-revision"
SERVICE_COMPONENTS = {"ModelService": "model-server", "FrontendService": "frontend"}
_BINDING_LABEL = "inference.foretoken.io/application-publisher-binding"
_SOURCE_CONSUMER_LABEL = "foretoken.io/application-source-consumer"
_SOURCE_ARCHIVE = "/source-evidence/inputs.tar.gz"


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


def application_consumers(kubectl: Kubectl) -> list[dict[str, Any]]:
    """Read current service selections and present workloads, including terminating consumers."""
    available = kubectl.api_resource_names("inference.foretoken.io")
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
    # Read independent consumer classes concurrently, completing every query before
    # callers can retire unreferenced applications or image pins.
    with ThreadPoolExecutor() as queries:
        pending = [queries.submit(kubectl.list_all_resources, kinds)] if kinds else []
        pending.extend(
            queries.submit(
                kubectl.list_all_resources,
                ("pods", "jobs", "replicasets", "deployments"),
                label_selector=label,
            )
            for label in (
                "app.kubernetes.io/name=foretoken-control-plane",
                "inference.foretoken.io/model-group",
                "inference.foretoken.io/frontend-service",
                "inference.foretoken.io/model-preparation-group",
                "foretoken.io/application-files=consumer",
                _SOURCE_CONSUMER_LABEL + "=true",
            )
        )
        return [obj for future in pending for obj in future.result()]


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

    def _manifest_path(self, application_url: str) -> str | None:
        prefix = self.endpoint.rstrip("/") + "/"
        if not application_url.startswith(prefix):
            return None
        relative = application_url.removeprefix(prefix).rstrip("/")
        return (
            f"/api/v1/namespaces/{self.namespace}/services/http:{self.name}:http/proxy/"
            + quote(relative, safe="/")
            + "/manifest.json"
        )

    def read_manifest(self, application_url: str) -> dict[str, Any] | None:
        """Read one immutable publication manifest, returning None only when it is absent."""
        path = self._manifest_path(application_url)
        if path is None:
            return None
        raw = self.kubectl.get_raw_if_not_found(path, "5s")
        if raw is None:
            return None
        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as error:
            raise DeploymentError(
                "application manifest returned invalid JSON"
            ) from error
        if not isinstance(manifest, dict):
            raise DeploymentError("application manifest must be an object")
        files = manifest.get("files")
        if not isinstance(files, list) or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not PurePosixPath(entry["path"]).parts
            or PurePosixPath(entry["path"]).is_absolute()
            or ".." in PurePosixPath(entry["path"]).parts
            or isinstance(entry.get("mode"), bool)
            or not isinstance(entry.get("mode"), int)
            or entry["mode"] < 0
            or entry["mode"] > 0o777
            or isinstance(entry.get("size"), bool)
            or not isinstance(entry.get("size"), int)
            or entry["size"] < 0
            for entry in files
        ):
            raise DeploymentError("application manifest has invalid file entries")
        return manifest

    def read_sources(self, application_url: str) -> dict[str, Any] | None:
        """Read retained build provenance for a current or retired application without publishing."""
        manifest = self.read_manifest(application_url)
        if manifest is None:
            prefix = self.endpoint.rstrip("/") + "/"
            if not application_url.startswith(prefix):
                return None
            relative = PurePosixPath(unquote(application_url.removeprefix(prefix).rstrip("/")))
            if len(relative.parts) != 2 or relative.is_absolute() or ".." in relative.parts:
                return None
            path = (
                f"/api/v1/namespaces/{self.namespace}/services/http:{self.name}:http/proxy/sources/"
                + quote(relative.as_posix(), safe="/") + ".json"
            )
            raw = self.kubectl.get_raw_if_not_found(path, "5s")
            if raw is None:
                return None
            try:
                manifest = json.loads(raw)
            except json.JSONDecodeError as error:
                raise DeploymentError("source evidence manifest returned invalid JSON") from error
            if not isinstance(manifest, dict):
                raise DeploymentError("source evidence manifest must be an object")
        sources = manifest.get("sources")
        if sources is None:
            return None
        if not isinstance(sources, dict):
            raise DeploymentError("application source provenance must be an object")
        if "parts" in sources:
            if not isinstance(sources["parts"], dict):
                raise DeploymentError("application source parts must be an object")
            for part in sources["parts"].values():
                if part is None:
                    continue
                if not isinstance(part, dict):
                    raise DeploymentError("application source part must be an object or null")
                if "archive" in part and (not isinstance(part["archive"], str) or not part["archive"]):
                    raise DeploymentError("application source archive must be a nonempty URL")
        return sources

    def download_source_archive(self, archive_url: str, directory: Path, *, timeout: str) -> Path:
        """Copy source evidence from this origin into a caller-owned directory without extracting it."""
        prefix = self.endpoint.rstrip("/") + "/sources/"
        if not archive_url.startswith(prefix):
            raise DeploymentError("source archive is outside the installed application origin")
        relative = PurePosixPath(unquote(archive_url.removeprefix(prefix)))
        if (
            not relative.parts or relative.is_absolute() or ".." in relative.parts
            or any(character in relative.as_posix() for character in ("\\", "\u0000"))
        ):
            raise DeploymentError("source archive has an invalid relative path")
        path = (
            f"/api/v1/namespaces/{self.namespace}/services/http:{self.name}:http/proxy/sources/"
            + quote(relative.as_posix(), safe="/")
        )
        destination = directory.joinpath(*relative.parts)
        self.kubectl.download_raw(path, destination, timeout)
        return destination

    def source_images(self, objects: Iterable[dict[str, Any]]) -> set[str]:
        """Retain environments selected by service intent before controllers publish status."""
        references = set()
        for obj in objects:
            component = SERVICE_COMPONENTS.get(obj.get("kind"))
            revision = obj["metadata"].get("annotations", {}).get(SOURCE_REVISION)
            if component is None or not revision:
                continue
            if revision in {".", ".."} or any(char in revision for char in "/\\\\\x00"):
                raise DeploymentError("source revision must be a single directory segment")
            references.add(self.reference(component, revision))
        images = set()
        for reference in sorted(references):
            manifest = self.read_manifest(reference)
            if manifest is None:
                raise DeploymentError(f"selected application manifest is unavailable: {reference}")
            if "image" not in manifest:
                continue
            image = manifest["image"]
            if not isinstance(image, str) or not image.strip():
                raise DeploymentError("application manifest image must be a nonempty string")
            images.add(image)
        return images

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
        objects = application_consumers(self.kubectl)
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
        reuse_previous: bool = False,
        sources: dict[str, Any] | None = None,
        image: str | None = None,
        parts: list[str] | None = None,
    ) -> str:
        """Publish a compiler export with provenance and return the selected immutable revision."""
        command = [
            "python",
            "-c",
            files("foretoken").joinpath("application_publish.py").read_text(),
            source,
            f"{self.mount}/{component}/{revision}",
            builder.binding,
            json.dumps({
                "previous": f"{self.mount}/{component}/{previous}" if previous else None,
                "keep": self._retained_versions(references),
                "reuse_previous": reuse_previous,
                "sources": sources,
                "image": image,
                "parts": parts,
                "source_archive": _SOURCE_ARCHIVE,
                "archive_url": f"{self.endpoint}/sources/{component}/{revision}.tar.gz",
            }),
        ]
        if parts is not None and not previous:
            raise DeploymentError("partial publication requires a completed application")
        with self._publisher(
            command, builder.binding, timeout, builder=builder,
            source_revision=previous if parts is not None else "",
        ) as job:
            self._wait(job, timeout)
            result = self.kubectl.run(
                ["logs", "job/" + job["name"], "-n", self.namespace, "-c", "publish"]
            )
            return json.loads(result.stdout)["revision"]

    def retire_source(self, binding: str, references: set[str], *, timeout: str) -> bool:
        """Collect a stopped binding's runtime files on the prepared origin; return whether none remain.

        Published source archives and manifests remain available independently of runtime files.
        """
        command = [
            "python", "-c", files("foretoken").joinpath("application_publish.py").read_text(),
            "--retire-source", self.mount, binding,
            json.dumps(self._retained_versions(references)),
        ]
        with self._publisher(command, binding, timeout) as job:
            self._wait(job, timeout)
            result = self.kubectl.run(
                ["logs", "job/" + job["name"], "-n", self.namespace, "-c", "publish"]
            )
            return not json.loads(result.stdout)["retained"]

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
        source_revision: str = "",
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
        if builder is not None:
            # Read private inputs as their actual compiler owner. Only a read-only
            # archive crosses into the publisher; neither container changes input modes.
            pod = self.kubectl.get("pod", builder.name, builder.namespace)
            compiler = next(container for container in pod["spec"]["containers"] if container["name"] == "builder")
            compiler_security = {
                **pod["spec"].get("securityContext", {}),
                **compiler.get("securityContext", {}),
            }
            archive_mount = {"name": "source-evidence", "mountPath": str(PurePosixPath(_SOURCE_ARCHIVE).parent)}
            volumes.append({"name": "source-evidence", "emptyDir": {}})
            mounts.append({**archive_mount, "readOnly": True})
            job["spec"]["template"]["spec"]["initContainers"] = [{
                "name": "source-evidence",
                "image": self.client_image,
                "command": [
                    "python", "-c", files("foretoken").joinpath("application_publish.py").read_text(),
                    "--archive-inputs", builder.workspace, _SOURCE_ARCHIVE,
                ],
                "terminationMessagePolicy": "FallbackToLogsOnError",
                "securityContext": {
                    "runAsUser": compiler_security["runAsUser"],
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "compiler", "mountPath": builder.mount, "readOnly": True},
                    archive_mount,
                ],
            }]
        if source_revision:
            # Keep publisher cleanup identity while exposing its borrowed files
            # to the existing cross-binding application reference collection.
            labels[_SOURCE_CONSUMER_LABEL] = "true"
            annotations = {SOURCE_REVISION: source_revision}
            job["metadata"]["annotations"] = annotations
            job["spec"]["template"]["metadata"]["annotations"] = annotations
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
        self.kubectl.wait_deleted(resource, metadata, timeout)

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
                    for container in (
                        *pod.get("status", {}).get("initContainerStatuses", []),
                        *pod.get("status", {}).get("containerStatuses", []),
                    ):
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
