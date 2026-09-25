# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Persistent serving-log collection owned by the Kubernetes platform lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from foretoken.manifest import DeploymentError
from foretoken.platform.types import ReleaseRef

if TYPE_CHECKING:
    from foretoken.platform.helm import Helm


@dataclass(frozen=True)
class LogConfig:
    """Resolve the platform's collection choice, Loki retention, and persistent storage."""

    enabled: bool = True
    endpoint: str = ""
    retention: str = "336h"
    storage_class: str = ""
    storage_size: str = "50Gi"
    image_pull_secrets: tuple[str, ...] = ()


def log_config_from_values(values: tuple[dict[str, Any], ...]) -> LogConfig:
    """Read explicit log settings in Helm precedence order; omitted fields keep defaults."""
    resolved: dict[str, Any] = {}
    fields = {
        "enabled": "enabled",
        "endpoint": "endpoint",
        "retention": "retention",
        "storageClass": "storage_class",
        "storageSize": "storage_size",
    }
    for item in values:
        if "imagePullSecrets" in item:
            secrets = item["imagePullSecrets"]
            if not isinstance(secrets, list) or not all(
                isinstance(secret, dict) and isinstance(secret.get("name"), str)
                for secret in secrets
            ):
                raise DeploymentError("imagePullSecrets must be a list of Secret names")
            resolved["image_pull_secrets"] = tuple(secret["name"] for secret in secrets)
        observability = item.get("observability", {})
        if not isinstance(observability, dict):
            raise DeploymentError("observability must be a mapping")
        logs = observability.get("logs", {})
        if not isinstance(logs, dict):
            raise DeploymentError("observability.logs must be a mapping")
        for key, field in fields.items():
            if key in logs:
                expected = bool if key == "enabled" else str
                if not isinstance(logs[key], expected):
                    raise DeploymentError(f"observability.logs.{key} must be {expected.__name__}")
                resolved[field] = logs[key]
    config = LogConfig(**resolved)
    if config.endpoint:
        endpoint = urlsplit(config.endpoint)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise DeploymentError("observability.logs.endpoint must be a Loki HTTP(S) base URL")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise DeploymentError("observability.logs.endpoint must not contain credentials, query, or fragment")
    return config


def loki_values(config: LogConfig) -> dict[str, Any]:
    """Configure one upstream Loki instance whose PVC outlives serving Pods and uninstall."""
    persistence: dict[str, Any] = {
        "enabled": True,
        "size": config.storage_size,
        "enableStatefulSetAutoDeletePVC": False,
    }
    if config.storage_class:
        persistence["storageClass"] = config.storage_class
    return {
        "deploymentMode": "Monolithic",
        "imagePullSecrets": [{"name": name} for name in config.image_pull_secrets],
        "loki": {
            "auth_enabled": False,
            "commonConfig": {"replication_factor": 1},
            "storage": {"type": "filesystem"},
            "schemaConfig": {"configs": [{
                "from": "2024-04-01",
                "store": "tsdb",
                "object_store": "filesystem",
                "schema": "v13",
                "index": {"prefix": "index_", "period": "24h"},
            }]},
            "limits_config": {"retention_period": config.retention},
            "compactor": {
                "retention_enabled": True,
                "delete_request_store": "filesystem",
                "working_directory": "/var/loki/compactor",
            },
        },
        "singleBinary": {"replicas": 1, "sidecar": False, "persistence": persistence},
        "backend": {"replicas": 0},
        "read": {"replicas": 0},
        "write": {"replicas": 0},
        "gateway": {"enabled": False},
        "chunksCache": {"enabled": False},
        "resultsCache": {"enabled": False},
        "lokiCanary": {"enabled": False},
        "test": {"enabled": False},
        "minio": {"enabled": False},
        "sidecar": {"rules": {"enabled": False}},
        "serviceAccount": {"automountServiceAccountToken": False},
    }


def collector_values(
    endpoint: str, image_pull_secrets: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Collect labeled Foretoken containers on each node with persistent offsets and buffers."""
    target = urlsplit(endpoint)
    port = target.port or (443 if target.scheme == "https" else 80)
    uri = target.path.rstrip("/") + "/loki/api/v1/push"
    labels = ",".join((
        "job=foretoken",
        "namespace=$kubernetes['namespace_name']",
        "pod=$kubernetes['pod_name']",
        "pod_uid=$kubernetes['pod_id']",
        "container=$kubernetes['container_name']",
        "node=$kubernetes['host']",
        "stream=$stream",
        "model_group=$kubernetes['labels']['inference.foretoken.io/model-group']",
        "model_role=$kubernetes['labels']['inference.foretoken.io/model-role']",
        "frontend_service=$kubernetes['labels']['inference.foretoken.io/frontend-service']",
        "kv_group=$kubernetes['labels']['inference.foretoken.io/kv-group']",
    ))
    return {
        "kind": "DaemonSet",
        "testFramework": {"enabled": False},
        "imagePullSecrets": [{"name": name} for name in image_pull_secrets],
        "tolerations": [{"operator": "Exists"}],
        "daemonSetVolumes": [
            {"name": "varlog", "hostPath": {"path": "/var/log"}},
            {"name": "state", "hostPath": {
                "path": "/var/log/foretoken-log-collector", "type": "DirectoryOrCreate",
            }},
        ],
        "daemonSetVolumeMounts": [
            {"name": "varlog", "mountPath": "/var/log", "readOnly": True},
            {"name": "state", "mountPath": "/var/lib/fluent-bit"},
        ],
        "config": {
            "service": """[SERVICE]
    Daemon Off
    Flush 1
    Log_Level info
    Parsers_File /fluent-bit/etc/parsers.conf
    HTTP_Server On
    HTTP_Listen 0.0.0.0
    HTTP_Port 2020
    Health_Check On
    storage.path /var/lib/fluent-bit/buffer
    storage.sync full
""",
            "inputs": """[INPUT]
    Name tail
    Path /var/log/containers/*.log
    Tag kube.*
    multiline.parser docker, cri
    DB /var/lib/fluent-bit/tail.db
    DB.Sync Full
    Read_from_Head On
    Refresh_Interval 1
    storage.type filesystem
""",
            "filters": """[FILTER]
    Name kubernetes
    Match kube.*
    Merge_Log Off
    Labels On
    Annotations Off

[FILTER]
    Name grep
    Match kube.*
    Logical_Op OR
    Regex $kubernetes['labels']['inference.foretoken.io/model-group'] .+
    Regex $kubernetes['labels']['inference.foretoken.io/frontend-service'] .+
    Regex $kubernetes['labels']['inference.foretoken.io/kv-group'] .+
    Regex $kubernetes['labels']['app.kubernetes.io/name'] ^foretoken-control-plane$
""",
            "outputs": f"""[OUTPUT]
    Name loki
    Match kube.*
    Host {target.hostname}
    Port {port}
    URI {uri}
    TLS {'On' if target.scheme == 'https' else 'Off'}
    TLS.Verify On
    Labels {labels}
    Line_Format json
    Retry_Limit False
""",
        },
    }


class LogCollectionLifecycle:
    """Install, update, and remove only CLI-owned log services, retaining stored logs."""

    def __init__(self, helm: Helm) -> None:
        self._helm = helm
        self.loki = helm.loki_release()
        self.collector = helm.log_collector_release()

    def plan(self, config: LogConfig) -> tuple[tuple[str, str, str], ...]:
        """Validate release ownership and describe the requested collection change."""
        actions = []
        for release, needed, name in (
            (self.loki, config.enabled and not config.endpoint, "Loki"),
            (self.collector, config.enabled, "Log collector"),
        ):
            exists = self._helm.release_exists(release)
            if exists and needed and not self._helm.is_cli_managed(release):
                raise DeploymentError(
                    f"Helm release {release.display_name} is not managed by foretoken; "
                    "use its existing Helm lifecycle"
                )
            if needed:
                action = "Upgrade" if exists else "Install"
            else:
                action = "Preserve" if exists else "Skip"
            if release == self.collector and exists and not needed:
                action = "Remove" if self._helm.is_cleanup_managed(release) else "Preserve"
            detail = release.display_name
            if release == self.loki and config.endpoint:
                action, detail = "Reuse", config.endpoint
            actions.append((name, action, detail))
        return tuple(actions)

    def install(self, config: LogConfig, timeout: str) -> str:
        """Apply collection settings and return the Loki URL for Grafana provisioning."""
        endpoint = config.endpoint.rstrip("/") or (
            f"http://{self.loki.name}.{self.loki.namespace}.svc:3100"
        )
        if not config.enabled:
            if self._helm.release_exists(self.collector) and self._helm.is_cleanup_managed(self.collector):
                self._helm.uninstall(self.collector, timeout)
            storage_exists = (
                self._helm.release_exists(self.loki)
                and self._helm.is_cleanup_managed(self.loki)
            )
            return endpoint if config.endpoint or storage_exists else ""
        if not config.endpoint:
            self._helm.install_loki(self.loki, loki_values(config), timeout)
        self._helm.install_log_collector(
            self.collector, collector_values(endpoint, config.image_pull_secrets), timeout
        )
        return endpoint

    def managed_releases(self) -> tuple[ReleaseRef, ...]:
        """Return managed releases in shutdown order, collector before storage."""
        return tuple(
            release for release in (self.collector, self.loki)
            if self._helm.release_exists(release) and self._helm.is_cleanup_managed(release)
        )

    def uninstall(self, releases: tuple[ReleaseRef, ...], timeout: str) -> None:
        """Stop collection and storage without deleting retained Loki claims."""
        for release in releases:
            self._helm.uninstall(release, timeout)
