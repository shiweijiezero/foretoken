# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read retained captures with scoped Kubernetes readers and shared temporary-resource cleanup."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from importlib import resources
from typing import Any, BinaryIO, Self
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

from loguru import logger

from foretoken.deployment.instances import DEPLOYMENT_UID_LABEL
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.processes.execution import CommandExecution
from foretoken.profiling.reader import (
    CAPTURE_MOUNT_PATH,
    READER_DEPLOYMENT_UID_ENV,
    READER_NAMESPACE_ENV,
    READER_PORT,
    READER_SECRET_ENV,
    capture_path,
    capture_token,
)


@dataclass(frozen=True)
class CaptureFileSource:
    """Borrow one reader's scoped file URL; consumers must close before ProfileStorage releases it."""

    namespace: str
    claim_uid: str
    deployment_uid: str
    path: str
    reader_name: str
    url: str


@dataclass(frozen=True)
class _Reader:
    name: str
    claim_uid: str
    secret: str


class ViewerResources:
    """Share UID-fenced creation and rollback while each viewer resource owner keeps its own registry."""

    def __init__(self, kubectl: Kubectl, timeout: str, seconds: float) -> None:
        """Track temporary resources under the owner's already resolved Kubernetes timeout."""
        self.kubectl = kubectl
        self.timeout = timeout
        self.seconds = seconds
        self._created: list[tuple[str, str, str, str]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release only this owner's temporary UIDs, leaving borrowed resources and stored traces intact."""
        self._cleanup()

    def _object(self, kind: str, name: str, namespace: str) -> dict[str, Any] | None:
        """Read one identity without confusing Kubernetes access failure with absence."""
        output = self.kubectl.run(
            [
                "get",
                kind,
                name,
                "-n",
                namespace,
                "--ignore-not-found",
                "-o",
                "json",
                f"--request-timeout={self.timeout}",
            ],
            timeout=self.seconds,
        ).stdout.strip()
        return json.loads(output) if output else None

    def _create(self, document: dict[str, Any], resource: str) -> dict[str, Any]:
        """Finish accepted creation and record its UID before observing viewer cancellation."""
        if execution := CommandExecution.current():
            execution.check()
        # Once submitted, creation must return its identity before the handler can
        # stop; the viewer then releases the recorded UID after joining handlers.
        with CommandExecution(report_failure=False):
            created = json.loads(
                self.kubectl.run(
                    [
                        "create",
                        "-f",
                        "-",
                        "-o",
                        "json",
                        f"--request-timeout={self.timeout}",
                    ],
                    input_text=json.dumps(document),
                    timeout=self.seconds,
                ).stdout
            )
        metadata = created["metadata"]
        self._created.append(
            (resource, metadata["namespace"], metadata["name"], metadata["uid"])
        )
        return created

    def _cleanup(self, start: int = 0) -> None:
        """Delete only exact UIDs created here; API preconditions protect replacement objects."""
        for resource, namespace, name, uid in reversed(self._created[start:]):
            path = f"/api/v1/namespaces/{namespace}/{resource}/{name}"
            try:
                if self._object(resource, name, namespace) is None:
                    continue
                self.kubectl.run(
                    [
                        "delete",
                        "--raw",
                        path,
                        "-f",
                        "-",
                        f"--request-timeout={self.timeout}",
                    ],
                    input_text=json.dumps(
                        {
                            "apiVersion": "v1",
                            "kind": "DeleteOptions",
                            "preconditions": {"uid": uid},
                            "propagationPolicy": "Background",
                        }
                    ),
                    timeout=self.seconds,
                )
                if resource == "pods":
                    self.kubectl.run(
                        [
                            "wait",
                            f"pod/{name}",
                            "-n",
                            namespace,
                            "--for=delete",
                            f"--timeout={self.timeout}",
                        ],
                        timeout=self.seconds + 1,
                    )
            except DeploymentError as exc:
                logger.warning(
                    "Could not remove temporary profile viewer {}/{}: {}",
                    namespace,
                    name,
                    exc,
                )
        del self._created[start:]


class ProfileStorage(ViewerResources):
    """Own one viewer's capture readers while preserving retained traces and storage."""

    def __init__(
        self, kubectl: Kubectl, timeout: str, image: str | None = None
    ) -> None:
        """Load platform viewer settings before this CLI session creates any temporary resources."""
        seconds = timeout_seconds(timeout)
        if seconds <= 0:
            raise DeploymentError("profile viewer timeout must be positive")
        super().__init__(kubectl, timeout, seconds)
        registry = os.environ.get("FORETOKEN_DOCKER_IO_REGISTRY", "docker.io").rstrip(
            "/"
        )
        self.image = image or f"{registry}/library/python:3.12-slim"
        self._readers: dict[tuple[str, str, str], _Reader] = {}
        configs = json.loads(
            self.kubectl.run(
                [
                    "get",
                    "configmaps",
                    "--all-namespaces",
                    "-l",
                    "foretoken.io/profile-viewer=configuration",
                    "-o",
                    "json",
                    f"--request-timeout={self.timeout}",
                ],
                timeout=self.seconds,
            ).stdout
        )["items"]
        if len(configs) > 1:
            raise DeploymentError(
                "multiple platform profile-viewer configurations found"
            )
        self.viewer_config = configs[0]["data"] if configs else {}

    def list_files(
        self, namespace: str, claim_name: str, artifact_path: str,
        *, deployment_uid: str,
    ) -> list[dict[str, Any]]:
        """List nested traces relative to a capture directory, distinguishing missing storage."""
        try:
            run = capture_path(artifact_path, namespace, deployment_uid)
        except ValueError as exc:
            raise DeploymentError("invalid capture directory") from exc
        reader = self._reader(namespace, claim_name, deployment_uid)
        path = self._path(reader, namespace, "list", run)
        try:
            result = json.loads(
                self.kubectl.run(
                    [
                        "get",
                        "--raw",
                        path,
                        f"--request-timeout={self.timeout}",
                    ],
                    timeout=self.seconds,
                ).stdout
            )
        except DeploymentError as exc:
            token = capture_token(reader.secret, run)
            raise DeploymentError(str(exc).replace(token, "[redacted]")) from None
        if result.get("error") == "not_found":
            return []
        if result.get("error"):
            raise DeploymentError("capture files cannot be read by the storage viewer")
        return result["files"]

    def stream_file(
        self,
        namespace: str,
        claim_name: str,
        artifact_path: str,
        name: str,
        output: BinaryIO,
        *, deployment_uid: str,
    ) -> None:
        """Stream a listed trace to the viewer socket under kubectl's command deadline and cleanup."""
        reader = self._reader(namespace, claim_name, deployment_uid)
        path = self._path(reader, namespace, "file", artifact_path, name)
        try:
            self.kubectl.run(
                ["get", "--raw", path, f"--request-timeout={self.timeout}"],
                output=output,
                timeout=self.seconds,
            )
        except DeploymentError as error:
            token = capture_token(reader.secret, artifact_path)
            raise DeploymentError(str(error).replace(token, "[redacted]")) from None

    def file_source(
        self, namespace: str, claim_name: str, directory: str, filename: str,
        *, deployment_uid: str,
    ) -> CaptureFileSource:
        """Borrow an authenticated cluster file source; consumers must release it before this storage closes."""
        reader = self._reader(namespace, claim_name, deployment_uid)
        path = f"{capture_path(directory, namespace, deployment_uid)}/{filename}"
        query = urlsplit(
            self._path(reader, namespace, "file", directory, filename)
        ).query
        return CaptureFileSource(
            namespace=namespace,
            claim_uid=reader.claim_uid,
            deployment_uid=deployment_uid,
            path=path,
            reader_name=reader.name,
            url=f"http://{reader.name}.{namespace}.svc:{READER_PORT}/file?{query}",
        )

    def close(self) -> None:
        """Release capture readers after their native consumers close, preserving traces and claims."""
        super().close()
        self._readers.clear()

    def _placement(self, namespace: str, claim: dict[str, Any]) -> str | None:
        """Keep an occupied RWO claim on its current node; never evict an RWOP consumer."""
        modes = claim["spec"].get("accessModes", [])
        if "ReadWriteMany" in modes or "ReadOnlyMany" in modes:
            return None
        pods = json.loads(
            self.kubectl.run(
                [
                    "get",
                    "pods",
                    "-n",
                    namespace,
                    "-o",
                    "json",
                    f"--request-timeout={self.timeout}",
                ],
                timeout=self.seconds,
            ).stdout
        )["items"]
        users = [
            pod
            for pod in pods
            if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and any(
                volume.get("persistentVolumeClaim", {}).get("claimName")
                == claim["metadata"]["name"]
                for volume in pod["spec"].get("volumes", [])
            )
        ]
        if users and "ReadWriteOncePod" in modes:
            raise DeploymentError(
                "capture storage is still in use by a ReadWriteOncePod workload"
            )
        nodes = {
            pod["spec"]["nodeName"] for pod in users if pod["spec"].get("nodeName")
        }
        if len(nodes) > 1:
            raise DeploymentError(
                "capture storage has workloads on multiple nodes; cannot mount its RWO claim"
            )
        return next(iter(nodes), None)

    def _reader(self, namespace: str, claim_name: str, deployment_uid: str) -> _Reader:
        """Mount one deployment's existing capture scope without changing storage or workloads."""
        claim = self._object("pvc", claim_name, namespace)
        if claim is None:
            raise FileNotFoundError("capture storage no longer exists")
        key = namespace, claim_name, deployment_uid
        if key in self._readers:
            reader = self._readers[key]
            if reader.claim_uid != claim["metadata"]["uid"]:
                raise DeploymentError(
                    "capture storage was replaced; restart the viewer"
                )
            return reader
        if claim["spec"].get("volumeMode", "Filesystem") != "Filesystem":
            raise DeploymentError("capture viewer requires filesystem storage")
        if claim.get("status", {}).get("phase") != "Bound":
            raise DeploymentError("capture storage is not bound")
        node = self._placement(namespace, claim)
        uid, gid = _capture_storage_identity(claim)
        name = f"foretoken-profile-view-{uuid4().hex[:16]}"
        reader = _Reader(name, claim["metadata"]["uid"], secrets.token_hex(32))
        labels = {
            "app.kubernetes.io/name": "foretoken-profile-view",
            "foretoken.io/view-session": name,
        }
        if deployment_uid:
            labels[DEPLOYMENT_UID_LABEL] = deployment_uid
        metadata = {"name": name, "namespace": namespace, "labels": labels}
        start = len(self._created)
        try:
            script = resources.files("foretoken.profiling").joinpath("reader.py")
            script_mount = "/reader"
            config = self._create(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": metadata,
                    "data": {script.name: script.read_text()},
                },
                "configmaps",
            )
            owner = [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "name": name,
                    "uid": config["metadata"]["uid"],
                }
            ]
            pod_spec: dict[str, Any] = {
                "automountServiceAccountToken": False,
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": 5,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": uid,
                    "runAsGroup": gid,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "reader",
                        "image": self.image,
                        "command": ["python", "-B", f"{script_mount}/{script.name}"],
                        "env": [
                            {
                                "name": READER_SECRET_ENV,
                                "value": reader.secret,
                            },
                            {
                                "name": READER_NAMESPACE_ENV,
                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                            },
                            {"name": READER_DEPLOYMENT_UID_ENV, "value": deployment_uid},
                        ],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "ports": [{"name": "http", "containerPort": READER_PORT}],
                        "readinessProbe": {"tcpSocket": {"port": "http"}},
                        "volumeMounts": [
                            {
                                "name": "captures",
                                "mountPath": CAPTURE_MOUNT_PATH,
                                "readOnly": True,
                            },
                            {
                                "name": "reader",
                                "mountPath": script_mount,
                                "readOnly": True,
                            },
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "captures",
                        "persistentVolumeClaim": {
                            "claimName": claim_name,
                            "readOnly": True,
                        },
                    },
                    {"name": "reader", "configMap": {"name": name}},
                ],
            }
            if node is not None:
                pod_spec["affinity"] = {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchFields": [
                                        {
                                            "key": "metadata.name",
                                            "operator": "In",
                                            "values": [node],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            self._create(
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {**metadata, "ownerReferences": owner},
                    "spec": pod_spec,
                },
                "pods",
            )
            self._create(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {**metadata, "ownerReferences": owner},
                    "spec": {
                        "selector": labels,
                        "ports": [
                            {"port": READER_PORT, "targetPort": "http", "name": "http"}
                        ],
                    },
                },
                "services",
            )
            self.kubectl.run(
                [
                    "wait",
                    "-n",
                    namespace,
                    f"pod/{name}",
                    "--for=condition=Ready",
                    f"--timeout={self.timeout}",
                    f"--request-timeout={self.timeout}",
                ],
                timeout=self.seconds + 1,
            )
            current = self._object("pvc", claim_name, namespace)
            if current is None:
                raise FileNotFoundError("capture storage no longer exists")
            if current["metadata"]["uid"] != reader.claim_uid:
                raise DeploymentError(
                    "capture storage was replaced while mounting the viewer"
                )
        except BaseException:
            self._cleanup(start)
            raise
        self._readers[key] = reader
        return reader

    def _path(
        self,
        reader: _Reader,
        namespace: str,
        action: str,
        directory: str,
        name: str = "",
    ) -> str:
        """Keep remote credentials bound to the selected capture directory and out of the browser."""
        query = urlencode(
            {
                "root": directory,
                "token": capture_token(reader.secret, directory),
                "name": name,
            }
        )
        return f"/api/v1/namespaces/{quote(namespace, safe='')}/services/{reader.name}:http/proxy/{action}?{query}"


def _capture_storage_identity(claim: dict[str, Any]) -> tuple[int, int]:
    """Read the existing directory identity without changing capture storage ownership."""
    value = claim["metadata"].get("annotations", {}).get("inference.foretoken.io/directory-owner")
    if value is None:
        return 1000, 1000
    try:
        uid, gid = map(int, value.split(":"))
        if not 0 < uid < 2**32 - 1 or not 0 <= gid < 2**32 - 1:
            raise ValueError
    except ValueError as error:
        raise DeploymentError(f"invalid capture storage directory owner: {value}") from error
    return uid, gid
