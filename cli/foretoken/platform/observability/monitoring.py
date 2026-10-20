# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve and reconcile managed monitoring while preserving independent Prometheus and exporters."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass

from foretoken.accelerators.config import METAX_GPU_RESOURCES, NVIDIA_GPU_RESOURCE
from foretoken.accelerators.discovery import ExporterDiscovery
from foretoken.accelerators.metax import (
    MetaXExporterLifecycle,
    MetaXMetrics,
    MetaXMetricsDiscovery,
)
from foretoken.accelerators.nvidia import NvidiaMetrics, NvidiaMetricsDiscovery
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, ResourceRef, timeout_seconds
from foretoken.observability import PrometheusRef, select_prometheus
from foretoken.platform.helm_client import HelmClient, merge_values
from foretoken.platform.observability.resources import (
    mark_managed_metrics_scraper_namespace,
    unmark_managed_metrics_scraper_namespace,
)
from foretoken.platform.types import ReleaseRef


@dataclass(frozen=True)
class MonitoringPlan:
    """Resolved installation ownership and native exporter placement for one platform update."""

    prometheus_exists: bool
    reused_prometheus: PrometheusRef | None
    dcgm_exists: bool
    managed_nvidia: NvidiaMetrics | None
    managed_metax: MetaXMetrics | None
    labels: tuple[tuple[str, str], ...]
    monitor_namespaces: tuple[str, ...]
    anonymous_access: bool | None
    prometheus_decision: tuple[str, str]
    exporter_decisions: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class MonitoringRetirement:
    """Observed ownership used to preserve external releases during platform retirement."""

    dcgm_exists: bool
    dcgm_managed: bool
    prometheus_exists: bool
    prometheus_managed: bool
    metax_managed: bool


class MonitoringLifecycle:
    """Own native monitoring installation, exporter readiness, namespace access, and retirement."""

    def __init__(self, helm: HelmClient, kubectl: Kubectl) -> None:
        self._helm = helm
        self._kubectl = kubectl
        config = helm.config
        self.prometheus = ReleaseRef(config.prometheus.release_name, config.namespace)
        self.dcgm = ReleaseRef(config.dcgm_exporter.release_name, config.namespace)
        self.metax_exporter = MetaXExporterLifecycle(
            kubectl, config.management_label, config.metax_exporter_image, config.image_registry,
        )

    def resolve_install(
        self, discovery: ExporterDiscovery, requested_prometheus: str | None,
        anonymous_access: bool | None,
    ) -> MonitoringPlan:
        """Select managed or reusable telemetry before platform installation mutates dependencies."""
        helm = self._helm
        dcgm_exists = helm.release_exists(self.dcgm)
        if dcgm_exists and not helm.is_cli_managed(self.dcgm):
            raise DeploymentError(
                f"Helm release {self.dcgm.display_name} is not managed by foretoken; use its existing Helm lifecycle"
            )
        nvidia = NvidiaMetricsDiscovery(discovery).resolve(self._dcgm_daemonset() if dcgm_exists else None)
        metax_exists = bool(self.metax_exporter.managed_resources())
        metax = MetaXMetricsDiscovery(discovery).resolve(self.metax_exporter.daemonset if metax_exists else None)
        install_metax = metax is not None and metax.exporter is None
        if install_metax:
            self.metax_exporter.ensure_available()

        prometheus_exists = helm.release_exists(self.prometheus)
        selected = None
        if prometheus_exists:
            if not helm.is_cli_managed(self.prometheus):
                raise DeploymentError(
                    f"Helm release {self.prometheus.display_name} is not managed by foretoken; use its existing Helm lifecycle"
                )
            prometheus_action, prometheus_detail = "Upgrade", self.prometheus.display_name
        else:
            selected = select_prometheus(self._kubectl, helm.config.namespace, requested_prometheus)
            prometheus_action = "Install" if selected is None else "Reuse"
            prometheus_detail = self.prometheus.display_name if selected is None else f"{selected.namespace}/{selected.name}"
        install_prometheus = prometheus_exists or selected is None
        if not install_prometheus and anonymous_access is not None:
            raise DeploymentError(
                "observability.grafana.anonymousAccess configures only Grafana installed "
                "by foretoken; configure the reused monitoring stack through its owner"
            )
        exporters = tuple(
            (name, exporter) for name, exporter in (
                ("DCGM", nvidia.exporter if nvidia is not None else None),
                ("mxExporter", metax.exporter if metax is not None else None),
            )
            if exporter is not None
        )
        if selected is not None:
            discovery.require_prometheus_selection(selected, exporters)
            discovery.require_prometheus_targets(selected, exporters)

        if nvidia is None:
            nvidia_action = "Preserve" if dcgm_exists else "Skip"
            nvidia_detail = self.dcgm.display_name if dcgm_exists else f"no allocatable {NVIDIA_GPU_RESOURCE} resource"
            install_dcgm = False
        elif dcgm_exists:
            nvidia_action, nvidia_detail, install_dcgm = "Upgrade", self.dcgm.display_name, True
        elif nvidia.exporter is not None:
            nvidia_action = "Reuse"
            nvidia_detail = f"{nvidia.exporter.daemonset.namespace}/{nvidia.exporter.daemonset.display_name}"
            install_dcgm = False
        else:
            nvidia_action, nvidia_detail, install_dcgm = "Install", self.dcgm.display_name, True
        if metax is None:
            metax_action, metax_detail = "Skip", f"no allocatable {' or '.join(METAX_GPU_RESOURCES)} resource"
        elif install_metax:
            metax_action = "Upgrade" if metax_exists else "Install"
            metax_detail = f"{self.metax_exporter.namespace}/{self.metax_exporter.daemonset.name}"
        else:
            metax_action = "Reuse"
            metax_detail = f"{metax.exporter.daemonset.namespace}/{metax.exporter.daemonset.display_name}"
        namespaces = {
            helm.config.namespace,
            *(exporter.service_monitor.namespace for _, exporter in exporters),
            *({self.metax_exporter.namespace} if install_metax else set()),
        }
        return MonitoringPlan(
            prometheus_exists=prometheus_exists,
            reused_prometheus=selected,
            dcgm_exists=dcgm_exists,
            managed_nvidia=nvidia if install_dcgm else None,
            managed_metax=metax if install_metax else None,
            labels=() if selected is None else selected.additional_labels,
            monitor_namespaces=tuple(sorted(namespaces)),
            anonymous_access=anonymous_access,
            prometheus_decision=(prometheus_action, prometheus_detail),
            exporter_decisions=(
                ("NVIDIA DCGM Exporter", nvidia_action, nvidia_detail),
                ("MetaX mxExporter", metax_action, metax_detail),
            ),
        )

    def apply(self, plan: MonitoringPlan, timeout: str) -> PrometheusRef:
        """Install selected components and verify that Prometheus can select and scrape every exporter."""
        selected = plan.reused_prometheus
        if selected is None:
            self._install_prometheus(plan, timeout)
            mark_managed_metrics_scraper_namespace(self._kubectl, self.prometheus.namespace)
            resource = self._helm.chart_resource(
                self.prometheus, api_version="monitoring.coreos.com/v1", kind="Prometheus",
                chart_description="managed monitoring chart",
            )
            selected = PrometheusRef(resource.name, resource.namespace, ())
        if plan.managed_nvidia is not None:
            self._install_dcgm(plan.managed_nvidia, plan.labels, plan.dcgm_exists, timeout)
            # Helm permits one unavailable Pod; discovery requires Ready coverage on every GPU node.
            self._kubectl.rollout_status(self._dcgm_daemonset(), timeout)
        if plan.managed_metax is not None:
            self.metax_exporter.install(plan.managed_metax.node_names, plan.labels, timeout)
        if plan.reused_prometheus is None or plan.managed_nvidia is not None or plan.managed_metax is not None:
            discovery = ExporterDiscovery(self._kubectl, timeout)
            nvidia = NvidiaMetricsDiscovery(discovery).resolve()
            metax = MetaXMetricsDiscovery(discovery).resolve()
            exporters = tuple(
                (name, exporter) for name, exporter in (
                    ("DCGM", nvidia.exporter if nvidia is not None else None),
                    ("mxExporter", metax.exporter if metax is not None else None),
                )
                if exporter is not None
            )
            discovery.require_prometheus_selection(selected, exporters)
            discovery.require_prometheus_targets(selected, exporters, timeout_seconds=timeout_seconds(timeout))
        return selected

    def resolve_uninstall(self) -> MonitoringRetirement:
        """Observe cleanup ownership before the platform begins deleting managed releases."""
        dcgm_exists = self._helm.release_exists(self.dcgm)
        prometheus_exists = self._helm.release_exists(self.prometheus)
        return MonitoringRetirement(
            dcgm_exists=dcgm_exists,
            dcgm_managed=dcgm_exists and self._helm.is_cleanup_managed(self.dcgm),
            prometheus_exists=prometheus_exists,
            prometheus_managed=prometheus_exists and self._helm.is_cleanup_managed(self.prometheus),
            metax_managed=bool(self.metax_exporter.managed_resources()),
        )

    def uninstall(self, plan: MonitoringRetirement, timeout: str) -> Iterator[tuple[str, str, str]]:
        """Retire only owned telemetry, yielding each completed decision before the next operation."""
        if plan.dcgm_managed:
            self._helm.uninstall(self.dcgm, timeout)
            yield "NVIDIA DCGM Exporter", "Removed", self.dcgm.display_name
        if plan.prometheus_managed:
            self._helm.uninstall(self.prometheus, timeout)
            unmark_managed_metrics_scraper_namespace(self._kubectl, self.prometheus.namespace)
            yield "Prometheus", "Removed", self.prometheus.display_name
        if plan.metax_managed:
            self.metax_exporter.uninstall(timeout)
            yield "MetaX mxExporter", "Removed", f"{self.metax_exporter.namespace}/{self.metax_exporter.daemonset.name}"

    def _dcgm_daemonset(self) -> ResourceRef:
        """Resolve the installed DaemonSet used by discovery and rollout waits."""
        return self._helm.chart_resource(
            self.dcgm, api_version="apps/v1", kind="DaemonSet", chart_description="managed DCGM Exporter chart",
        )

    def _install_prometheus(self, plan: MonitoringPlan, timeout: str) -> None:
        """Install the native monitoring chart while retaining administrator customizations."""
        helm = self._helm
        anonymous_settings = {"enabled": True, "org_role": "Viewer"}
        values = {
            "grafana": {
                "defaultDashboardsEnabled": False,
                "grafana.ini": {
                    "auth.anonymous": anonymous_settings,
                    "users": {"default_theme": "light"},
                    "plugins": {"preinstall_auto_update": False},
                },
            },
        }
        if plan.prometheus_exists:
            values = merge_values(values, helm.release_user_values(self.prometheus))
        if plan.anonymous_access is not None:
            values = merge_values(values, {"grafana": {"grafana.ini": {"auth.anonymous": {
                **anonymous_settings, "enabled": plan.anonymous_access,
            }}}})
        selected_namespaces = plan.monitor_namespaces
        if not selected_namespaces:
            raise DeploymentError("managed Prometheus requires a monitor namespace")
        namespace_selector = (
            {"matchLabels": {"kubernetes.io/metadata.name": selected_namespaces[0]}}
            if len(selected_namespaces) == 1 else {
                "matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": list(selected_namespaces)}],
            }
        )
        rule_selector = {"matchLabels": {"app.kubernetes.io/name": "foretoken-control-plane"}}
        chart = helm.config.prometheus
        args = helm.upgrade_args(self.prometheus, chart.source, timeout)
        args.extend([
            "--set", "prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false",
            "--set-json", "prometheus.prometheusSpec.serviceMonitorSelector={}",
            "--set-json", "prometheus.prometheusSpec.serviceMonitorNamespaceSelector=" + json.dumps(namespace_selector, separators=(",", ":")),
            "--set", "prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false",
            "--set-json", "prometheus.prometheusSpec.ruleSelector=" + json.dumps(rule_selector, separators=(",", ":")),
            "--set-json", "prometheus.prometheusSpec.ruleNamespaceSelector={}",
            "--set-string", "alertmanager.alertmanagerSpec.alertmanagerConfigMatcherStrategy.type=OnNamespaceExceptForAlertmanagerNamespace",
            "--set-string", "grafana.sidecar.datasources.defaultDatasourceScrapeInterval=5s",
            "--set-json", "kube-state-metrics.metricLabelsAllowlist=" + json.dumps([
                "pods=[inference.foretoken.io/model-group,inference.foretoken.io/model-role,inference.foretoken.io/pd-pipeline-scope]",
            ], separators=(",", ":")),
        ])
        args.extend(["--reset-values", "--values", "-"])
        with helm.prepared_chart(chart.source, chart.version) as prepared:
            args[3] = str(prepared)
            # Keep quota and throttled-time counters in upstream defaults; explicit relabelings still win.
            defaults = json.loads(helm.run(["show", "values", str(prepared), "--jsonpath", "{@}"]).stdout)
            rules = defaults["kubelet"]["serviceMonitor"]["cAdvisorMetricRelabelings"]
            for rule in rules:
                if rule.get("sourceLabels") != ["__name__"] or rule.get("action") != "drop":
                    continue
                expression = rule.get("regex", "")
                if expression == "container_spec.*":
                    rule["regex"] = "container_spec_(cpu_shares|memory_.*)"
                elif expression.startswith("container_cpu_("):
                    rule["regex"] = expression.replace("cfs_throttled_seconds_total|", "")
            helm.set_chart_defaults(prepared, {"kubelet.serviceMonitor.cAdvisorMetricRelabelings": rules})
            helm.set_chart_image_sources(
                prepared,
                ("prometheusOperator.image", "prometheusOperator.admissionWebhooks.patch.image",
                 "prometheusOperator.admissionWebhooks.deployment.image", "prometheusOperator.prometheusConfigReloader.image",
                 "prometheusOperator.thanosImage", "prometheus.prometheusSpec.image", "alertmanager.alertmanagerSpec.image",
                 "thanosRuler.thanosRulerSpec.image", "grafana.image", "grafana.sidecar.image", "grafana.initChownData.image",
                 "grafana.downloadDashboardsImage", "grafana.testFramework.image", "kube-state-metrics.image", "prometheus-node-exporter.image"),
                release=self.prometheus, overrides=values,
                subcharts=("grafana", "kube-state-metrics", "prometheus-node-exporter"),
                version_prefixes=("kube-state-metrics.image", "prometheus-node-exporter.image"),
                distroless_paths=("prometheus-node-exporter.image",),
            )
            helm.run(args, input_text=json.dumps(values))

    def _install_dcgm(
        self, metrics: NvidiaMetrics, labels: tuple[tuple[str, str], ...], reuse_values: bool, timeout: str,
    ) -> None:
        """Apply native DCGM configuration and retain explicit environments across upgrades."""
        helm = self._helm
        chart = helm.config.dcgm_exporter
        args = helm.upgrade_args(self.dcgm, chart.source, timeout)
        stored = helm.release_user_values(self.dcgm) if reuse_values else {}
        if reuse_values:
            args.extend(["--reset-values", "--values", "-"])
        args.extend([
            "--set", "serviceMonitor.enabled=true", "--set-string", "serviceMonitor.interval=5s",
            "--set-string", "serviceMonitor.scrapeTimeout=4s", "--set", "kubernetes.enablePodLabels=true",
            "--set-json", 'kubernetes.podLabelAllowlistRegex=["^inference\\\\.foretoken\\\\.io/.*$"]',
            "--set-string", "customMetrics=" + helm.config.dcgm_metrics.replace(",", "\\,"),
            "--set-json", "securityContext.capabilities.add=[]",
        ])
        if metrics.visible_devices is not None:
            environment = [entry for entry in stored.get("extraEnv") or [] if entry["name"] != "NVIDIA_VISIBLE_DEVICES"]
            environment.append({"name": "NVIDIA_VISIBLE_DEVICES", "value": metrics.visible_devices})
            args.extend(["--set-json", "extraEnv=" + json.dumps(environment)])
        if labels:
            args.extend(["--set-json", "serviceMonitor.additionalLabels=" + json.dumps(dict(labels), separators=(",", ":"))])
        node_selector = {} if metrics.node_selector is None else {metrics.node_selector[0]: metrics.node_selector[1]}
        args.extend(["--set-json", "nodeSelector=" + json.dumps(node_selector, separators=(",", ":"))])
        with helm.prepared_chart(chart.source, chart.version) as prepared:
            args[3] = str(prepared)
            helm.set_chart_image_sources(prepared, ("image",), release=self.dcgm, overrides=stored)
            helm.run(args, input_text=json.dumps(stored) if reuse_values else None)
