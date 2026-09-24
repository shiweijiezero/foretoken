# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own temporary Kubernetes readers and native viewing sessions for retained captures."""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from importlib import resources
from typing import Any, BinaryIO, Self
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from foretoken.kubernetes import Kubectl, timeout_seconds
from foretoken.manifest import DeploymentError
from foretoken.profiling.reader import (
    CAPTURE_MOUNT_PATH,
    READER_PORT,
    READER_SECRET_ENV,
    capture_path,
    capture_token,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Reader:
    name: str
    claim_uid: str
    secret: str


class ProfileStorage:
    """Own one viewer's readers and native sessions; preserve captures and storage."""

    def __init__(
        self, kubectl: Kubectl, timeout: str, image: str | None = None
    ) -> None:
        self.kubectl = kubectl
        self.timeout = timeout
        self.seconds = timeout_seconds(timeout)
        if self.seconds <= 0:
            raise DeploymentError("profile viewer timeout must be positive")
        registry = os.environ.get("FORETOKEN_DOCKER_IO_REGISTRY", "docker.io").rstrip(
            "/"
        )
        self.image = image or f"{registry}/library/python:3.12-slim"
        self._readers: dict[tuple[str, str], _Reader] = {}
        self._created: list[tuple[str, str, str, str]] = []
        self._nsight_sessions: dict[tuple[str, str, str], str] = {}
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

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
        """Record each created UID immediately so partial startup has the same cleanup owner."""
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

    def _reader(self, namespace: str, claim_name: str) -> _Reader:
        """Mount an existing claim without changing its binding, contents, or workload."""
        claim = self._object("pvc", claim_name, namespace)
        if claim is None:
            raise FileNotFoundError("capture storage no longer exists")
        key = namespace, claim_name
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
        name = f"foretoken-profile-view-{uuid4().hex[:16]}"
        reader = _Reader(name, claim["metadata"]["uid"], secrets.token_hex(32))
        labels = {
            "app.kubernetes.io/name": "foretoken-profile-view",
            "foretoken.io/view-session": name,
        }
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
                    "runAsUser": 65532,
                    "runAsGroup": 65532,
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
                            }
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

    def open_nsight(
        self, namespace: str, claim_name: str, directory: str, filename: str
    ) -> str:
        """Start an authenticated official viewer for one retained report; close owns cleanup."""
        image = self.viewer_config.get("nsightImage")
        if not image:
            raise DeploymentError(
                "NVIDIA Nsight Viewer is not configured; update the platform or download the report"
            )
        reader = self._reader(namespace, claim_name)
        report = f"{capture_path(directory)}/{filename}"
        key = namespace, reader.claim_uid, report
        if key in self._nsight_sessions:
            return self._nsight_sessions[key]
        # The reader already enforces PVC placement and verifies its identity. Co-locate
        # the native viewer so an occupied RWO claim remains on the same node.
        reader_pod = self._object("pod", reader.name, namespace)
        if reader_pod is None:
            raise DeploymentError("capture reader was removed; restart the viewer")
        config = self._object("configmap", reader.name, namespace)
        if config is None:
            raise DeploymentError(
                "capture reader configuration was removed; restart the viewer"
            )
        name = f"foretoken-nsight-{uuid4().hex[:16]}"
        labels = {
            "app.kubernetes.io/name": "foretoken-nsight-view",
            "foretoken.io/view-session": name,
        }
        metadata = {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "ownerReferences": [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "name": reader.name,
                    "uid": config["metadata"]["uid"],
                }
            ],
        }
        token = secrets.token_urlsafe(32)
        # TURN credentials are separate from HTTP access; Pion accepts word characters.
        turn_password = secrets.token_hex(32)
        cookie = name.replace("-", "_")
        # A session cookie protects HTTP and WebSocket traffic without putting Basic
        # credentials in the URL, which prevents the official UI's relative fetches.
        proxy_config = f"""server {{
    listen 8081;
    absolute_redirect off;
    access_log off;
    add_header Referrer-Policy no-referrer always;
    location = /session/{token} {{
        add_header Set-Cookie "{cookie}={token}; Path=/; HttpOnly; SameSite=Lax";
        add_header Referrer-Policy no-referrer always;
        return 302 /;
    }}
    location / {{
        if ($cookie_{cookie} != "{token}") {{ return 403; }}
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1d;
    }}
}}
"""
        start = len(self._created)
        try:
            self._create(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": metadata,
                    "stringData": {
                        "turn-password": turn_password,
                        "default.conf": proxy_config,
                    },
                },
                "secrets",
            )
            service = self._create(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": metadata,
                    "spec": {
                        "type": "NodePort",
                        "selector": labels,
                        "ports": [
                            {"name": "http", "port": 8080, "targetPort": "http"},
                            {"name": "turn", "port": 3478, "targetPort": "turn"},
                        ],
                    },
                },
                "services",
            )
            # Reuse Kubernetes-assigned ports for the external service so k3s/k3d
            # load balancers sharing a node IP can host several viewing sessions.
            ports = service["spec"]["ports"]
            for port in ports:
                port["port"] = port["nodePort"]
            self.kubectl.run(
                [
                    "patch",
                    f"service/{name}",
                    "-n",
                    namespace,
                    "--type=merge",
                    "-p",
                    json.dumps({"spec": {"type": "LoadBalancer", "ports": ports}}),
                    f"--request-timeout={self.timeout}",
                ],
                timeout=self.seconds,
            )
            public_ports = {port["name"]: port["port"] for port in ports}
            self.kubectl.run(
                [
                    "wait",
                    f"service/{name}",
                    "-n",
                    namespace,
                    "--for=jsonpath={.status.loadBalancer.ingress}",
                    f"--timeout={self.timeout}",
                    f"--request-timeout={self.timeout}",
                ],
                timeout=self.seconds + 1,
            )
            service = self._object("service", name, namespace)
            if service is None:
                raise DeploymentError("NVIDIA Nsight Viewer service was removed")
            ingress = service["status"]["loadBalancer"]["ingress"][0]
            host = ingress.get("ip") or ingress["hostname"]
            credential = {"secretKeyRef": {"name": name, "key": "turn-password"}}
            self._create(
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": metadata,
                    "spec": {
                        "affinity": {
                            "nodeAffinity": {
                                "requiredDuringSchedulingIgnoredDuringExecution": {
                                    "nodeSelectorTerms": [
                                        {
                                            "matchFields": [
                                                {
                                                    "key": "metadata.name",
                                                    "operator": "In",
                                                    "values": [
                                                        reader_pod["spec"]["nodeName"]
                                                    ],
                                                }
                                            ]
                                        }
                                    ],
                                }
                            }
                        },
                        "automountServiceAccountToken": False,
                        "restartPolicy": "Never",
                        "imagePullSecrets": json.loads(
                            self.viewer_config["imagePullSecrets"]
                        ),
                        "containers": [
                            {
                                "name": "viewer",
                                "image": image,
                                "env": [
                                    {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"},
                                    {"name": "ENCODER", "value": "vp9enc"},
                                    {
                                        "name": "DEVTOOL_CMD",
                                        "value": "host-linux-x64/nsys-ui --fullscreen /reports/selected.nsys-rep",
                                    },
                                    {
                                        "name": "NSIGHT_PASSTHROUGH_ENV_VARS",
                                        "value": "DEVTOOL_CMD,ENCODER",
                                    },
                                    {"name": "WEB_USERNAME", "value": ""},
                                    {"name": "TURN_USERNAME", "value": "foretoken"},
                                    {"name": "TURN_PASSWORD", "valueFrom": credential},
                                    {"name": "CLIENT_TURN_HOST", "value": host},
                                    {
                                        "name": "CLIENT_TURN_PORT",
                                        "value": str(public_ports["turn"]),
                                    },
                                    {
                                        "name": "CLIENT_TURN_USERNAME",
                                        "value": "foretoken",
                                    },
                                    {
                                        "name": "CLIENT_TURN_PASSWORD",
                                        "valueFrom": credential,
                                    },
                                ],
                                "ports": [
                                    {"name": "viewer", "containerPort": 8080},
                                    {"name": "turn", "containerPort": 3478},
                                ],
                                "readinessProbe": {"tcpSocket": {"port": "viewer"}},
                                "volumeMounts": [
                                    {
                                        "name": "report",
                                        "mountPath": "/reports/selected.nsys-rep",
                                        "subPath": report,
                                        "readOnly": True,
                                    },
                                    {"name": "shm", "mountPath": "/dev/shm"},
                                ],
                            },
                            {
                                "name": "access",
                                "image": self.viewer_config["proxyImage"],
                                "ports": [{"name": "http", "containerPort": 8081}],
                                "readinessProbe": {"tcpSocket": {"port": "http"}},
                                "volumeMounts": [
                                    {
                                        "name": "access",
                                        "mountPath": "/etc/nginx/conf.d",
                                        "readOnly": True,
                                    }
                                ],
                                "securityContext": {"allowPrivilegeEscalation": False},
                            },
                        ],
                        "volumes": [
                            {
                                "name": "report",
                                "persistentVolumeClaim": {
                                    "claimName": claim_name,
                                    "readOnly": True,
                                },
                            },
                            {"name": "shm", "emptyDir": {"medium": "Memory"}},
                            {
                                "name": "access",
                                "secret": {
                                    "secretName": name,
                                    "items": [
                                        {"key": "default.conf", "path": "default.conf"}
                                    ],
                                },
                            },
                        ],
                    },
                },
                "pods",
            )
            self.kubectl.run(
                [
                    "wait",
                    f"pod/{name}",
                    "-n",
                    namespace,
                    "--for=condition=Ready",
                    f"--timeout={self.timeout}",
                    f"--request-timeout={self.timeout}",
                ],
                timeout=self.seconds + 1,
            )
            authority = f"[{host}]" if ":" in host else host
            origin = f"http://{authority}:{public_ports['http']}"
            # Pod readiness can precede EndpointSlice and load-balancer propagation.
            # Hand the browser a URL only after its authenticated HTTP route responds.
            deadline = time.monotonic() + self.seconds
            request = Request(origin + "/", headers={"Cookie": f"{cookie}={token}"})
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    with urlopen(request, timeout=min(5, remaining)) as response:
                        if response.status == 200:
                            break
                except (URLError, OSError):
                    pass
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            else:
                raise DeploymentError(
                    "NVIDIA viewer address is not reachable; check the cluster LoadBalancer route"
                )
            url = f"{origin}/session/{token}"
            self._nsight_sessions[key] = url
            return url
        except BaseException:
            self._cleanup(start)
            raise

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

    def list_files(
        self, namespace: str, claim_name: str, artifact_path: str
    ) -> list[dict[str, Any]]:
        """List nested traces relative to a capture directory, distinguishing missing storage."""
        try:
            run = capture_path(artifact_path)
        except ValueError as exc:
            raise DeploymentError("invalid capture directory") from exc
        reader = self._reader(namespace, claim_name)
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
            raise FileNotFoundError("capture directory was not found")
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
    ) -> None:
        """Stream a listed trace through kubectl without materializing it in CLI memory."""
        reader = self._reader(namespace, claim_name)
        path = self._path(reader, namespace, "file", artifact_path, name)
        timed_out = threading.Event()
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                self.kubectl.command(
                    [
                        "get",
                        "--raw",
                        path,
                        f"--request-timeout={self.timeout}",
                    ]
                ),
                stdout=subprocess.PIPE,
                stderr=errors,
            )

            def expire() -> None:
                timed_out.set()
                process.kill()

            timer = threading.Timer(self.seconds, expire)
            timer.daemon = True
            timer.start()
            try:
                assert process.stdout is not None
                with process.stdout:
                    while chunk := process.stdout.read(1024 * 1024):
                        output.write(chunk)
                status = process.wait()
                if timed_out.is_set():
                    raise DeploymentError("timed out reading capture file")
                if status:
                    errors.seek(0)
                    detail = errors.read().decode(errors="replace").strip()
                    token = capture_token(reader.secret, artifact_path)
                    raise DeploymentError(
                        "cannot read capture file: "
                        + detail.replace(token, "[redacted]")
                    )
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                process.wait()

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
                    "Could not remove temporary profile viewer %s/%s: %s",
                    namespace,
                    name,
                    exc,
                )
        del self._created[start:]

    def close(self) -> None:
        """Release this viewer's temporary readers without deleting traces, claims or namespaces."""
        self._cleanup()
        self._readers.clear()
        self._nsight_sessions.clear()
