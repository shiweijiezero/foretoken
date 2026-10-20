# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Persistent serving-log collection owned by the Kubernetes platform lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import yaml

from foretoken.errors import DeploymentError
from foretoken.network_sources import select_platform_image_sources
from foretoken.platform.helm_client import HelmClient, image_repository_tag, merge_values
from foretoken.platform.types import ReleaseRef


@dataclass(frozen=True)
class LogConfig:
    """Resolve the platform's collection choice, Loki retention, and persistent storage."""

    enabled: bool = True
    endpoint: str = ""
    retention: str = "336h"
    storage_class: str = ""
    initial_size: str = "5Gi"
    max_size: str = ""
    image_pull_secrets: tuple[str, ...] = ()


class LogCollectionLifecycle:
    """Install, update, and remove only CLI-owned log services, retaining stored logs."""

    def __init__(self, helm: HelmClient) -> None:
        self._helm = helm
        self.loki = ReleaseRef(helm.config.loki.release_name, helm.config.namespace)
        self.collector = ReleaseRef(helm.config.log_collector.release_name, helm.config.namespace)

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

    def install(self, config: LogConfig, timeout: str) -> tuple[str, str]:
        """Return Grafana's URL and the managed StatefulSet selected for volume expansion."""
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
            if not config.endpoint and not storage_exists:
                return "", ""
        else:
            if not config.endpoint:
                self._install_loki(loki_values(config), timeout)
            self._install_collector(
                collector_values(endpoint, self.collector, config.image_pull_secrets), timeout,
            )
        statefulset = (
            self._helm.chart_resource(
                self.loki, api_version="apps/v1", kind="StatefulSet", chart_description="managed Loki chart",
            ).name
            if config.max_size and not config.endpoint else ""
        )
        return endpoint, statefulset

    def managed_releases(self) -> tuple[ReleaseRef, ...]:
        """Return managed releases in shutdown order, collector before storage."""
        return tuple(
            release for release in (self.collector, self.loki)
            if self._helm.release_exists(release) and self._helm.is_cleanup_managed(release)
        )

    def uninstall(self, releases: tuple[ReleaseRef, ...], timeout: str) -> None:
        """Stop collection and storage, retaining node checkpoints and Loki claims."""
        for release in releases:
            self._helm.uninstall(release, timeout)

    def _install_loki(self, values: dict[str, Any], timeout: str) -> None:
        """Update native Loki values without changing its immutable creation-size template."""
        helm = self._helm
        stored = helm.stored_chart_values(self.loki)
        if stored:
            values["singleBinary"]["persistence"]["size"] = stored["singleBinary"]["persistence"]["size"]
        values = merge_values(stored, values)
        chart = helm.config.loki
        with helm.prepared_chart(chart.source, chart.version) as prepared:
            args = helm.upgrade_args(self.loki, str(prepared), timeout)
            helm.set_chart_image_sources(prepared, ("loki.image",), release=self.loki, overrides=values)
            args.extend(["--reset-values", "--values", "-"])
            helm.run(args, input_text=yaml.safe_dump(values))

    def _install_collector(self, values: dict[str, Any], timeout: str) -> None:
        """Upgrade native collectors while retaining checkpoint and unsent-buffer locations."""
        helm = self._helm
        chart = helm.config.log_collector
        stored = helm.stored_chart_values(self.collector)
        for volume in stored.get("daemonSetVolumes", []):
            if volume["name"] == "state" and "hostPath" in volume:
                values["daemonSetVolumes"] = [
                    {**item, "hostPath": volume["hostPath"]} if item["name"] == "state" else item
                    for item in values["daemonSetVolumes"]
                ]
                break
        values = merge_values(stored, values)
        with helm.prepared_chart(chart.source, chart.version) as prepared:
            args = helm.upgrade_args(self.collector, str(prepared), timeout)
            args.extend(["--reset-values", "--values", "-"])
            if "image" not in values:
                # cr.fluentbit.io fronts Docker Hub; resolve its canonical publication.
                helm.set_chart_defaults(prepared, {"image.repository": "docker.io/fluent/fluent-bit"})
                for document in helm.render_chart(args, input_text=yaml.safe_dump(values)):
                    if document["kind"] == "DaemonSet":
                        image = document["spec"]["template"]["spec"]["containers"][0]["image"]
                        selected = select_platform_image_sources(
                            {"image": image}, helm.config.image_registry,
                            installed_images=helm.installed_chart_images(self.collector),
                        )["image"]
                        repository, tag = image_repository_tag(selected)
                        helm.set_chart_defaults(prepared, {"image.repository": repository, "image.tag": tag})
            helm.run(args, input_text=yaml.safe_dump(values))


def log_config_from_values(values: tuple[dict[str, Any], ...]) -> LogConfig:
    """Read explicit log settings in Helm precedence order; omitted fields keep defaults."""
    resolved: dict[str, Any] = {}
    fields = {
        "enabled": "enabled",
        "endpoint": "endpoint",
        "retention": "retention",
        "storageClass": "storage_class",
        "initialSize": "initial_size",
        "maxSize": "max_size",
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
        "size": config.initial_size,
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
    endpoint: str, release: ReleaseRef, image_pull_secrets: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Configure node collectors with release-scoped checkpoints retained across Pod replacement."""
    target = urlsplit(endpoint)
    port = target.port or (443 if target.scheme == "https" else 80)
    uri = target.path.rstrip("/") + "/loki/api/v1/push"
    labels = (
        "job=foretoken,"
        "namespace=$kubernetes['namespace_name'],"
        "pod=$kubernetes['pod_name'],"
        "pod_uid=$kubernetes['pod_id'],"
        "container=$kubernetes['container_name'],"
        "node=$kubernetes['host'],"
        "stream=$stream,"
        "model_group=$kubernetes['labels']['inference.foretoken.io/model-group'],"
        "model_role=$kubernetes['labels']['inference.foretoken.io/model-role'],"
        "frontend_service=$kubernetes['labels']['inference.foretoken.io/frontend-service'],"
        "kv_group=$kubernetes['labels']['inference.foretoken.io/kv-group']"
    )
    return {
        "kind": "DaemonSet",
        "testFramework": {"enabled": False},
        "imagePullSecrets": [{"name": name} for name in image_pull_secrets],
        "tolerations": [{"operator": "Exists"}],
        "daemonSetVolumes": [
            {"name": "varlog", "hostPath": {"path": "/var/log"}},
            {"name": "state", "hostPath": {
                "path": f"/var/lib/foretoken/log-collector/{release.namespace}/{release.name}",
                "type": "DirectoryOrCreate",
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
