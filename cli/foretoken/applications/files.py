# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own short-lived file publishers and address the platform's persistent origin."""

from __future__ import annotations

import json
import math
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

from loguru import logger

from foretoken.build.records import (
    SOURCE_BUILD_KEY,
    WORKSPACE_LABEL,
    WORKSPACE_SELECTION_KEY,
)
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, ResourceRef, timeout_seconds
from foretoken.processes.execution import CommandExecution, cleanup_commands

if TYPE_CHECKING:
    from foretoken.build.cluster.compiler import CompilerWorkspace

SOURCE_REVISION = "inference.foretoken.io/source-revision"
SERVICE_COMPONENTS = {"ModelService": "model-server", "FrontendService": "frontend"}
APPLICATION_PUBLISHER_BINDING_LABEL = "inference.foretoken.io/application-publisher-binding"
_SOURCE_CONSUMER_LABEL = "foretoken.io/application-source-consumer"
_SOURCE_ARCHIVE = "/source-evidence/inputs.tar.gz"


@dataclass(frozen=True)
class ApplicationPublication:
    """Describe one completed export selected by a source-build publication cohort."""

    component: str
    source: str
    revision: str
    previous: str
    sources: dict[str, Any] | None
    image: str | None
    parts: list[str] | None
    previous_image: str | None = None


@dataclass(frozen=True)
class PublishedApplications:
    """Return completed application revisions and the optional portable build selected by their publisher."""

    revisions: dict[str, str]
    completed_build: dict[str, Any] | None


class ApplicationFiles:
    """Resolve the installed file origin without taking ownership of model storage."""

    def __init__(self, kubectl: Kubectl, namespace: str) -> None:
        """Resolve origin access for readers; prepare selects current storage before publisher operations."""
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
        with kubectl.until(time.monotonic() + timeout_seconds(timeout)):
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

    def publish_batch(
        self,
        builder: CompilerWorkspace,
        publications: Sequence[ApplicationPublication],
        references: set[str] | None,
        *,
        timeout: str,
        build_record: dict[str, Any] | None = None,
    ) -> PublishedApplications:
        """Publish a build cohort with one input archive and Job, committing its build record last."""
        started = time.monotonic()
        logger.info("Publishing applications: {}", ", ".join(item.component for item in publications))
        selected = []
        borrowed = []
        for publication in publications:
            if publication.parts is not None and not publication.previous:
                raise DeploymentError("partial publication requires a completed application")
            component, revision = publication.component, publication.revision
            if publication.parts is not None:
                borrowed.append(self.reference(component, publication.previous))
            selected.append({
                "component": component,
                "source": publication.source,
                "destination": f"{self.mount}/{component}/{revision}",
                "previous": f"{self.mount}/{component}/{publication.previous}" if publication.previous else None,
                "sources": publication.sources,
                "image": publication.image,
                "parts": publication.parts,
                "previous_image": publication.previous_image,
                "archive_url": f"{self.endpoint}/sources/{component}/{revision}.tar.gz",
            })
        relative = None
        if build_record is not None:
            relative = "sources/builds/" + uuid.uuid4().hex
            descriptor = builder.root + "/completed-build.json"
            builder.run(
                ["sh", "-ec", 'cat > "$1"', "record", descriptor],
                container=builder.artifact_container, input_text=json.dumps(build_record),
            )
        selection = {
            "applications": selected,
            "binding": builder.binding,
            "keep": self._retained_versions(references),
            "source_archive": _SOURCE_ARCHIVE,
            "build": {
                "descriptor": descriptor,
                "destination": self.mount + "/" + relative,
                "url": self.endpoint + "/" + relative,
            } if relative is not None else None,
        }

        def inputs() -> dict[str, str]:
            """Retain partial-publication bases through the existing workspace reference protocol."""
            data = {"input.json": json.dumps(selection)}
            if borrowed:
                data[WORKSPACE_SELECTION_KEY] = json.dumps({"applications": borrowed, "images": []})
            return data

        with builder.cancellation, self._publisher(
            ["--batch", "/selection/input.json"], builder.binding, timeout,
            builder=builder, inputs=inputs,
        ) as (job, deadline), self.kubectl.until(deadline):
            self._wait(job, deadline)
            result = json.loads(self.kubectl.run(
                ["logs", "job/" + job["name"], "-n", self.namespace, "-c", "publish"]
            ).stdout)
        logger.info("Applications published in {:.1f}s", time.monotonic() - started)
        completed = {
            "url": self.endpoint + "/" + relative + "/build.json",
            "applications": {component: self.reference(component, revision) for component, revision in result.items()},
            "images": build_record["build"]["images"],
        } if relative is not None else None
        return PublishedApplications(result, completed)

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
        arguments = [
            "--release",
            source,
            self.mount,
            revision,
            binding,
            "/selection/input.json",
        ]
        def inputs() -> dict[str, str]:
            """Capture retained releases only after this installer owns the publication slot."""
            return {
                "input.json": json.dumps({
                    "keep": self._retained_versions(references()),
                    "previous": previous(),
                }),
            }

        with self._publisher(
            arguments,
            binding,
            timeout,
            name="foretoken-release-" + self.claim_uid,
            inputs=inputs,
            credentials_secret=credentials_secret,
        ) as (job, deadline):
            with self.kubectl.until(deadline):
                self._wait(job, deadline)
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
        """Retain source environments selected by instance or service intent before materialization."""
        references = set()
        for obj in objects:
            if obj.get("kind") == "InferenceDeployment":
                component = "control-plane"
                revision = obj.get("spec", {}).get("controllerSourceRevision")
            else:
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
        arguments: list[str],
        binding: str,
        timeout: str,
        *,
        builder: CompilerWorkspace | None = None,
        name: str = "",
        inputs: Callable[[], dict[str, str]] | None = None,
        credentials_secret: str = "",
    ) -> Iterator[tuple[dict[str, Any], float]]:
        """Own a publisher Job and its inputs, supplying executable code only after Job creation is acknowledged."""
        wait_for_slot = bool(name)
        name = name or "foretoken-publish-" + uuid.uuid4().hex[:12]
        input_name = "foretoken-publish-input-" + uuid.uuid4().hex[:12]
        seconds = math.ceil(timeout_seconds(timeout))
        execution = builder.cancellation if builder is not None else CommandExecution.current()
        job = self._publisher_job(
            arguments, binding, name=name, input_name=input_name, seconds=seconds,
            builder=builder,
            credentials_secret=credentials_secret,
        )
        deadline = time.monotonic() + seconds
        metadata = None
        uncertain_create = False

        def stop() -> None:
            """Recover a lost create response without claiming another release publisher."""
            receipt = metadata
            if receipt is None:
                if not uncertain_create:
                    return
                current = self.kubectl.get_if_exists("job", name, self.namespace)
                if current is None or current["metadata"].get("labels", {}).get(APPLICATION_PUBLISHER_BINDING_LABEL) != binding:
                    return
                if not any(
                    owner["kind"] == "PersistentVolumeClaim" and owner["uid"] == self.claim_uid
                    for owner in current["metadata"].get("ownerReferences", [])
                ):
                    return
                receipt = current["metadata"]
            self._delete_owned("jobs", receipt, timeout)

        try:
            if execution is not None:
                execution.register(stop)
            while True:
                if execution is not None:
                    execution.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeploymentError(f"application publication {self.namespace}/{name} exceeded its creation deadline")
                try:
                    uncertain_create = True
                    created = self.kubectl.run(
                        ["create", "-f", "-", "-o", "json"], input_text=json.dumps(job), timeout=remaining,
                    )
                    metadata = json.loads(created.stdout)["metadata"]
                    uncertain_create = False
                    break
                except DeploymentError as exc:
                    # An occupied name proves that this request created nothing.
                    if "AlreadyExists" not in str(exc):
                        raise
                    uncertain_create = False
                    if not wait_for_slot:
                        raise
                    if time.monotonic() >= deadline:
                        raise DeploymentError(
                            f"application release operation {self.namespace}/{name} is occupied after {timeout}"
                        ) from None
                    time.sleep(min(1, max(0.0, deadline - time.monotonic())))
            if execution is not None:
                execution.check()
            # A create whose response was lost can still commit after cancellation.
            # Without these UID-owned inputs, that Job cannot read exports or collect
            # publications and expires through its native deadline and TTL.
            with self.kubectl.until(deadline):
                data = inputs() if inputs is not None else {}
                data["publish.py"] = files("foretoken.applications").joinpath("publish.py").read_text()
                selection = {
                    "apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {
                        "name": input_name, "namespace": self.namespace,
                        "labels": {WORKSPACE_LABEL: binding} if WORKSPACE_SELECTION_KEY in data else {},
                        "ownerReferences": [{
                            "apiVersion": "batch/v1", "kind": "Job",
                            "name": metadata["name"], "uid": metadata["uid"],
                        }],
                    },
                    "immutable": True,
                    "data": data,
                }
                self.kubectl.run(["create", "-f", "-"], input_text=json.dumps(selection))
            if execution is not None:
                execution.check()
            yield metadata, deadline
        finally:
            if execution is not None:
                execution.unregister(stop)
                execution.cleanup(stop, sys.exception())
            else:
                with cleanup_commands(timeout=seconds):
                    stop()

    def _publisher_job(
        self,
        arguments: list[str],
        binding: str,
        *,
        name: str,
        input_name: str,
        seconds: int,
        builder: CompilerWorkspace | None,
        credentials_secret: str,
    ) -> dict[str, Any]:
        """Render publisher storage and identity without taking ownership of borrowed compiler files."""
        labels = {
            "foretoken.io/application-files": "publisher",
            APPLICATION_PUBLISHER_BINDING_LABEL: binding,
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
        mounts.extend([
            {"name": "temporary", "mountPath": "/tmp"},
            {"name": "selection", "mountPath": "/selection", "readOnly": True},
        ])
        volumes.extend([
            {"name": "temporary", "emptyDir": {}},
            {"name": "selection", "configMap": {"name": input_name}},
        ])
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
                                "command": ["python", "/selection/publish.py", *arguments],
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
            execution_volume = "compiler"
            if builder.execution_claim != builder.claim:
                execution_volume = "execution"
                volumes.append({
                    "name": execution_volume,
                    "persistentVolumeClaim": {"claimName": builder.execution_claim, "readOnly": True},
                })
            archive_mount = {"name": "source-evidence", "mountPath": str(PurePosixPath(_SOURCE_ARCHIVE).parent)}
            volumes.append({"name": "source-evidence", "emptyDir": {}})
            mounts.append({**archive_mount, "readOnly": True})
            job["spec"]["template"]["spec"]["initContainers"] = [{
                "name": "source-evidence",
                "image": self.client_image,
                "command": [
                    "python", "/selection/publish.py", "--archive-inputs", builder.workspace, _SOURCE_ARCHIVE,
                ],
                "terminationMessagePolicy": "FallbackToLogsOnError",
                "securityContext": {
                    "runAsUser": compiler_security["runAsUser"],
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": execution_volume, "mountPath": builder.execution_mount, "readOnly": True},
                    {"name": "selection", "mountPath": "/selection", "readOnly": True},
                    archive_mount,
                ],
            }]
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
        return job

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

    def _wait(self, job: dict[str, Any], deadline: float) -> None:
        """Observe publication within its original Job deadline and retain failure diagnostics."""
        name = job["name"]
        while time.monotonic() < deadline:
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
                try:
                    pods = self.kubectl.list_resources(
                        ("pods",), self.namespace,
                        label_selector="batch.kubernetes.io/controller-uid=" + job["uid"],
                    )
                except DeploymentError as error:
                    messages.append(f"Pod diagnostics unavailable: {error}")
                else:
                    for pod in pods:
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
            time.sleep(min(1, max(0.0, deadline - time.monotonic())))
        raise DeploymentError(
            f"application publication {self.namespace}/{name} exceeded its publication deadline"
        )


def application_references(objects: Iterable[dict[str, Any]]) -> set[str]:
    """Read retained application identities from service state and workload projections."""
    references: set[str] = set()
    for obj in objects:
        spec = obj.get("spec", {})
        if obj.get("kind") == "InferenceDeployment" and spec.get("controllerSourceRevision"):
            references.add(spec["controllerSourceRevision"])
        if obj.get("kind") == "ConfigMap" and WORKSPACE_SELECTION_KEY in obj.get("data", {}):
            references.update(json.loads(obj["data"][WORKSPACE_SELECTION_KEY])["applications"])
        if obj.get("kind") == "ConfigMap" and "controller-selection.json" in obj.get("data", {}):
            selection = json.loads(obj["data"]["controller-selection.json"])
            references.update(application_references([selection["template"]["controller"]]))
        template = spec.get("template", {})
        metadata = obj["metadata"]
        template_metadata = template.get("metadata", {})
        if metadata.get("labels", {}).get("foretoken.io/platform") == "configuration":
            values = json.loads(obj["data"]["deploymentValues"])
            if source_build := obj["data"].get(SOURCE_BUILD_KEY):
                references.update(json.loads(source_build).get("completedBuild", {}).get("applications", {}).values())
            references.update(filter(None, (
                values["controller"]["applicationURL"],
                values["frontend"]["applicationURL"],
                values["runtime"]["vllm"]["applicationURL"],
            )))
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
            "inferencedeployments",
            "modelservices",
            "frontendservices",
            "modelpools",
            "modelgroups",
            "videotasks",
        )
        if kind + ".inference.foretoken.io" in available
    )
    # A failed reference read cancels sibling queries before their executor joins.
    # The caller may defer collection, so this observation has its own failure scope.
    parent = CommandExecution.current()
    execution = CommandExecution(parent.control_timeout if parent is not None else None)
    try:
        if parent is not None:
            parent.register(execution.cancel)
        with ThreadPoolExecutor() as queries, execution:
            pending = [queries.submit(copy_context().run, execution.run_stage, kubectl.list_all_resources, kinds)] if kinds else []
            pending.extend(queries.submit(
                copy_context().run, execution.run_stage, kubectl.list_all_resources, ("configmaps",),
                label_selector=selector,
            ) for selector in (
                "foretoken.io/platform=configuration",
                "app.kubernetes.io/component=deployment-controller",
                WORKSPACE_LABEL,
            ))
            pending.extend(
                queries.submit(
                    copy_context().run, execution.run_stage, kubectl.list_all_resources,
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
    finally:
        if parent is not None:
            parent.unregister(execution.cancel)


def remove_application_jobs(kubectl: Kubectl, binding: str, timeout: str) -> None:
    """Stop abandoned writers before their workstation reuses its compiler output."""
    kubectl.run(
        [
            "delete",
            "jobs",
            "--all-namespaces",
            "--selector",
            f"foretoken.io/application-files=publisher,{APPLICATION_PUBLISHER_BINDING_LABEL}={binding}",
            "--ignore-not-found",
            "--cascade=foreground",
            "--wait=true",
            "--timeout=" + timeout,
        ]
    )
