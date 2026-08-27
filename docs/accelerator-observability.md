<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Accelerator observability

English | [简体中文](accelerator-observability_zh.md)

Use this guide to collect accelerator hardware metrics in a Foretoken Kubernetes cluster. Foretoken reuses vendor-provided exporters and keeps hardware telemetry separate from inference-backend metrics. The NVIDIA DCGM adapter is the only adapter packaged in this release.

## Measurement boundary

```text
Accelerator hardware
  → vendor exporter
  → ServiceMonitor
  → Prometheus
  → vendor dashboard

Inference request
  → Frontend /metrics
  → model-server /metrics
  → Prometheus
  → Foretoken service dashboard
```

Accelerator exporters own utilization, device memory, power, temperature, interconnect, and hardware-error metrics. Frontend and model-server endpoints own request, routing, scheduler, cache, and backend-native inference metrics. Foretoken does not copy hardware metrics into model-server `/metrics`.

## Supported adapters

| Accelerator | Exporter | Repository assets | Dashboard |
| --- | --- | --- | --- |
| NVIDIA GPU | NVIDIA DCGM Exporter | `deploy/observability/accelerators/nvidia-dcgm` | `Foretoken NVIDIA GPU 运行状态` |

Follow the [NVIDIA DCGM adapter guide](accelerators/nvidia-dcgm.md) to install or reuse that exporter. Support for another vendor requires a separate, validated adapter; the table does not imply that unlisted accelerators are already supported.

## Adapter contract

Every adapter preserves the vendor-native Prometheus metrics emitted by its exporter. Foretoken and the ServiceMonitor do not apply a metric allowlist or rename metric families. Collector selection and required privileges remain adapter-owned and must be documented. The ServiceMonitor adds these target labels:

| Label | Meaning | NVIDIA DCGM value |
| --- | --- | --- |
| `foretoken_observability_source` | Common hardware source | `accelerator` |
| `foretoken_accelerator_vendor` | Accelerator vendor | `nvidia` |
| `foretoken_accelerator_exporter` | Exporter implementation | `dcgm` |

Prometheus discovers repository-provided adapters in the `accelerator-monitoring` namespace. The namespace is a shared integration boundary, not a claim that different vendors use the same exporter, raw metric names, units, or device labels. Vendor dashboards query their own native metric families and include all three target labels.

The current release does not provide cross-vendor normalized metric families or a common hardware dashboard. The NVIDIA dashboard queries DCGM-native metrics. Other accelerator vendors are not currently supported and require a separate adapter.

## Install the monitoring foundation

Set up Prometheus Operator and Grafana first by following [Observability](../observability/README.md). The supplied kube-prometheus-stack values discover ServiceMonitors in only three namespaces:

- `monitoring` for the monitoring stack;
- `foretoken-platform` for Foretoken-owned monitors;
- `accelerator-monitoring` for accelerator adapters.

The trusted Prometheus namespace must have this label so that Foretoken and adapter NetworkPolicies permit metric scrapes:

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite
```

If Prometheus must discover an additional ServiceMonitor namespace, copy the values file and extend only the namespace allowlist. Moving the supplied monitoring stack or adapter to another namespace also requires updating its Helm command and namespace-scoped manifests. Do not use cluster-wide ServiceMonitor discovery as a shortcut.

## Verify an adapter

List the adapter ServiceMonitor:

```bash
kubectl get servicemonitor --namespace accelerator-monitoring
```

In Prometheus, query all accelerator targets:

```promql
up{foretoken_observability_source="accelerator"}
```

Each installed adapter should return `1` and include its `foretoken_accelerator_vendor` and `foretoken_accelerator_exporter` labels. Then open the adapter-specific Grafana dashboard and verify its device inventory against the vendor tool on the node.

Missing native metric families do not mean a numeric value of zero. Hardware model, driver, firmware, exporter version, and enabled collector fields can all affect which series exist.

## Add another adapter

An adapter contribution should contain:

1. a vendor-supported exporter deployment or a documented integration with an existing operator;
2. a ServiceMonitor using the three target labels above;
3. a NetworkPolicy that accepts scrapes only from namespaces labeled `inference.foretoken.io/metrics-scraper=true`;
4. a vendor-specific dashboard that preserves stable device identity;
5. bilingual install, verification, troubleshooting, and cleanup instructions;
6. rendered-manifest and dashboard-query checks in CI.

Keep vendor lifecycle, security privileges, CRDs, and native metric semantics inside the adapter. Do not add accelerator exporter settings to the Foretoken inference chart or re-export hardware metrics from model-server.

## Related guides

- [Observability](../observability/README.md)
- [NVIDIA DCGM adapter](accelerators/nvidia-dcgm.md)
- [Deploy Foretoken with k3d](k3d-deployment.md)
