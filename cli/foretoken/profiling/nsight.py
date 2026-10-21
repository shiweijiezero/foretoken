# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own official Nsight viewer sessions while borrowing scoped capture readers."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from foretoken.deployment.instances import DEPLOYMENT_UID_LABEL
from foretoken.errors import DeploymentError
from foretoken.profiling.storage import (
    CaptureFileSource,
    ProfileStorage,
    ViewerResources,
)


@dataclass(frozen=True)
class _NsightSession:
    """Keep one native session's identity and credentials consistent across its resource manifests."""

    metadata: dict[str, Any]
    token: str
    turn_password: str
    report_url: str

    @classmethod
    def prepare(cls, source: CaptureFileSource, reader_uid: str) -> Self:
        """Compile a borrowed source for NsightViewerSessions without creating Kubernetes objects."""
        name = f"foretoken-nsight-{uuid4().hex[:16]}"
        labels = {
            "app.kubernetes.io/name": "foretoken-nsight-view",
            "foretoken.io/view-session": name,
        }
        if source.deployment_uid:
            labels[DEPLOYMENT_UID_LABEL] = source.deployment_uid
        metadata = {
            "name": name,
            "namespace": source.namespace,
            "labels": labels,
            "ownerReferences": [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "name": source.reader_name,
                    "uid": reader_uid,
                }
            ],
        }
        # TURN credentials are separate from HTTP access; Pion accepts word characters.
        return cls(metadata, secrets.token_urlsafe(32), secrets.token_hex(32), source.url)


class NsightViewerSessions(ViewerResources):
    """Own native viewer resources; the caller must close these sessions before their borrowed storage."""

    def __init__(self, storage: ProfileStorage) -> None:
        """Borrow resolved reader access and configuration without discovering platform settings again."""
        super().__init__(storage.kubectl, storage.timeout, storage.seconds)
        self.storage = storage
        self._sessions: dict[tuple[str, str, str], str] = {}

    def open(
        self, namespace: str, claim_name: str, directory: str, filename: str,
        *, deployment_uid: str,
    ) -> str:
        """Return a ready native URL to the HTTP handler, retaining only this owner's session resources."""
        image = self.storage.viewer_config.get("nsightImage")
        if not image:
            raise DeploymentError(
                "NVIDIA Nsight Viewer is not configured; update the platform or download the report"
            )
        source = self.storage.file_source(
            namespace, claim_name, directory, filename, deployment_uid=deployment_uid
        )
        key = source.namespace, source.claim_uid, source.path
        if key in self._sessions:
            return self._sessions[key]

        # The reader remains storage-owned; native resources only borrow its scoped file URL and UID.
        config = self._object("configmap", source.reader_name, source.namespace)
        if config is None:
            raise DeploymentError(
                "capture reader configuration was removed; restart the viewer"
            )
        session = _NsightSession.prepare(source, config["metadata"]["uid"])
        name = session.metadata["name"]
        start = len(self._created)
        try:
            # Publish credentials and the address before compiling the native viewer's connection settings.
            self._create(self._secret(session), "secrets")
            service = self._create(self._service(session), "services")
            patch = load_balancer_patch(service)
            self.kubectl.run(
                [
                    "patch",
                    f"service/{name}",
                    "-n",
                    namespace,
                    "--type=merge",
                    "-p",
                    json.dumps(patch),
                    f"--request-timeout={self.timeout}",
                ],
                timeout=self.seconds,
            )
            public_ports = {port["name"]: port["port"] for port in patch["spec"]["ports"]}
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
            self._create(self._viewer_pod(session, image, host, public_ports), "pods")
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
            url = self._wait_until_accessible(session, host, public_ports)
            self._sessions[key] = url
            return url
        except BaseException:
            self._cleanup(start)
            raise

    def close(self) -> None:
        """Release native viewers before ProfileStorage closes the readers they borrow."""
        super().close()
        self._sessions.clear()

    def _secret(self, session: _NsightSession) -> dict[str, Any]:
        """Build the private report source, TURN credential, and authenticated proxy configuration."""
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": session.metadata,
            "stringData": {
                "turn-password": session.turn_password,
                "default.conf": self._proxy_configuration(session),
                "report-url": session.report_url,
            },
        }

    def _service(self, session: _NsightSession) -> dict[str, Any]:
        """Request Kubernetes-assigned HTTP and TURN ports before publishing the viewer address."""
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": session.metadata,
            "spec": {
                "type": "NodePort",
                "selector": session.metadata["labels"],
                "ports": [
                    {"name": "http", "port": 8080, "targetPort": "http"},
                    {"name": "turn", "port": 3478, "targetPort": "turn"},
                ],
            },
        }

    def _viewer_pod(
        self,
        session: _NsightSession,
        image: str,
        host: str,
        public_ports: Mapping[str, int],
    ) -> dict[str, Any]:
        """Compile report transfer, the official viewer, and its access proxy for the published address."""
        name = session.metadata["name"]
        credential = {"secretKeyRef": {"name": name, "key": "turn-password"}}
        # The reader authorizes the copy; the native viewer receives only session-local report bytes.
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": session.metadata,
            "spec": {
                "automountServiceAccountToken": False,
                "restartPolicy": "Never",
                "imagePullSecrets": json.loads(self.storage.viewer_config["imagePullSecrets"]),
                "initContainers": [
                    {
                        "name": "report-copy",
                        "image": self.storage.image,
                        "command": [
                            "python",
                            "-I",
                            "-B",
                            "-c",
                            """import os, shutil, sys
from urllib.error import URLError
from urllib.request import urlopen

try:
    with urlopen(os.environ['FORETOKEN_PROFILE_REPORT_URL'], timeout=float(sys.argv[1])) as source:
        with open('/reports/selected.nsys-rep', 'xb') as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            if target.tell() != int(source.headers['Content-Length']):
                raise OSError('incomplete report transfer')
            os.fchmod(target.fileno(), 0o444)
except (URLError, OSError) as error:
    raise SystemExit(f'Unable to copy the selected report: {error}')
""",
                            str(self.seconds),
                        ],
                        "env": [
                            {
                                "name": "FORETOKEN_PROFILE_REPORT_URL",
                                "valueFrom": {
                                    "secretKeyRef": {"name": name, "key": "report-url"}
                                },
                            }
                        ],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "runAsGroup": 1000,
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "volumeMounts": [{"name": "report", "mountPath": "/reports"}],
                    }
                ],
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
                            {"name": "CLIENT_TURN_USERNAME", "value": "foretoken"},
                            {"name": "CLIENT_TURN_PASSWORD", "valueFrom": credential},
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
                                "subPath": "selected.nsys-rep",
                                "readOnly": True,
                            },
                            {"name": "shm", "mountPath": "/dev/shm"},
                        ],
                    },
                    {
                        "name": "access",
                        "image": self.storage.viewer_config["proxyImage"],
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
                    {"name": "report", "emptyDir": {}},
                    {"name": "shm", "emptyDir": {"medium": "Memory"}},
                    {
                        "name": "access",
                        "secret": {
                            "secretName": name,
                            "items": [{"key": "default.conf", "path": "default.conf"}],
                        },
                    },
                ],
            },
        }

    def _wait_until_accessible(
        self, session: _NsightSession, host: str, public_ports: Mapping[str, int]
    ) -> str:
        """Return the authenticated URL only after the native HTTP route responds, beyond Pod readiness."""
        authority = f"[{host}]" if ":" in host else host
        origin = f"http://{authority}:{public_ports['http']}"
        # EndpointSlice and LoadBalancer propagation can outlast the Pod's TCP readiness.
        deadline = time.monotonic() + self.seconds
        cookie = session.metadata["name"].replace("-", "_")
        request = Request(origin + "/", headers={"Cookie": f"{cookie}={session.token}"})
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
        return f"{origin}/session/{session.token}"

    def _proxy_configuration(self, session: _NsightSession) -> str:
        """Authorize HTTP and WebSocket requests without URL credentials that break native relative fetches."""
        cookie = session.metadata["name"].replace("-", "_")
        return f"""server {{
    listen 8081;
    absolute_redirect off;
    access_log off;
    add_header Referrer-Policy no-referrer always;
    location = /session/{session.token} {{
        add_header Set-Cookie "{cookie}={session.token}; Path=/; HttpOnly; SameSite=Lax";
        add_header Referrer-Policy no-referrer always;
        return 302 /;
    }}
    location / {{
        if ($cookie_{cookie} != "{session.token}") {{ return 403; }}
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1d;
    }}
}}
"""


def load_balancer_patch(service: dict[str, Any]) -> dict[str, Any]:
    """Reuse assigned NodePorts as public ports so viewing sessions can share a node's LoadBalancer IP."""
    ports = service["spec"]["ports"]
    for port in ports:
        port["port"] = port["nodePort"]
    return {"spec": {"type": "LoadBalancer", "ports": ports}}
