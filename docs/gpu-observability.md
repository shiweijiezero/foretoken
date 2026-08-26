<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Monitor NVIDIA GPUs

English | [简体中文](gpu-observability_zh.md)

This guide adds continuous NVIDIA GPU hardware monitoring to a Foretoken Kubernetes cluster. It installs NVIDIA DCGM Exporter as a cluster add-on, lets Prometheus discover its complete configured output, and provisions a Chinese Grafana dashboard.

The GPU path is intentionally separate from inference metrics:

```text
NVIDIA driver and GPUs on each node
  → DCGM Exporter DaemonSet (:9400/metrics)
  → ServiceMonitor in gpu-monitoring
  → Prometheus in monitoring
  → Foretoken GPU 运行状态 dashboard in Grafana
```

The model-server `/metrics` endpoint continues to expose backend-native inference metrics such as vLLM request latency, scheduler state, and KV cache usage. DCGM Exporter owns hardware metrics such as utilization, framebuffer memory, power, temperature, clocks, throttling, and hardware errors. Neither source replaces the other.

## Supported boundary

The repository provides:

- kube-prometheus-stack values that discover ServiceMonitors only in `monitoring`, `foretoken-platform`, and `gpu-monitoring`;
- hardened values for NVIDIA DCGM Exporter chart `4.8.2` and image `4.5.3-4.8.2-distroless`;
- an additive NetworkPolicy for the standalone exporter that allows Pods from `monitoring` to reach TCP port `9400` when the cluster CNI enforces NetworkPolicy;
- a datasource-independent Chinese Grafana dashboard; and
- a curated set of base hardware fields without `DCGM_FI_PROF_*` profiling fields.

This setup does not:

- put GPU metrics into the model-server `/metrics` endpoint;
- reserve a schedulable `nvidia.com/gpu` resource for the exporter;
- attribute a physical GPU sample to a Foretoken ModelGroup or Pod;
- install alerts, notification receivers, or a Benchmark collector; or
- enable DCGM profiling metrics or replace PyTorch Profiler and Nsight.

The exporter runs as UID `1000`, drops all Linux capabilities, and does not receive `SYS_ADMIN`. The repository also clears the upstream chart's `system-node-critical` priority because an optional monitor must not receive a priority that can preempt inference Pods. It still consumes the chart's CPU and memory resources: each exporter Pod requests `100m` CPU and `128Mi` memory and has limits of `200m` and `512Mi`.

Foretoken disables the exporter's Kubernetes Pod mapping with the explicit `--kubernetes=false` CLI argument. Base hardware telemetry therefore has no Pod or container attribution. Chart `4.8.2` still renders a read-only hostPath mount for `/var/lib/kubelet/pod-resources` even when mapping is disabled; the process does not poll it, but the mount cannot be removed through this chart version's values. Treat that as a pinned upstream-chart limitation, not as a capability this configuration uses.

## Prerequisites

Before installing this path, confirm that:

- NVIDIA drivers and NVIDIA Container Toolkit work on every selected GPU node;
- the container runtime exposes the GPUs to containers;
- `kubectl` and Helm are configured for the intended cluster;
- the cluster has a CNI capable of enforcing NetworkPolicy if ingress isolation is required; and
- you know whether the cluster already runs GPU Operator or another DCGM Exporter.

For k3d, first complete [Deploy Foretoken with k3d](k3d-deployment.md). The repository k3d configuration makes NVIDIA the default runtime, so the standalone exporter does not need a `runtimeClassName` override.

Do not install a second exporter when GPU Operator already provides one. Reuse the existing exporter, include its ServiceMonitor namespace in a copy of `deploy/observability/kube-prometheus-stack-values.yaml`, and configure its ServiceMonitor to add this target label:

```yaml
relabelings:
  - targetLabel: foretoken_observability_source
    replacement: dcgm
```

The supplied dashboard uses that stable label instead of depending on a Helm release-specific `job` name.

Before reusing an existing exporter, inspect one raw GPU series and confirm that it provides the uppercase `UUID` label expected by this dashboard. DCGM Exporter's legacy namespace mode can use lowercase `uuid`; disable legacy mode or adapt the dashboard before reuse if its label contract differs. Also confirm that the metric families used by the dashboard are enabled.

The supplied `dcgm-exporter-network-policy.yaml` matches the labels of the standalone chart used in this guide. It does not automatically select a GPU Operator-managed exporter. When reusing another exporter, inspect its actual Pod labels and existing policies, then provide a platform-owned ingress policy if one is required.

## 1. Install or configure Prometheus and Grafana

Reuse a platform monitoring stack when one already exists. The commands below are for a Foretoken development cluster and pin the version validated by this repository:

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --version 88.5.4 \
  --namespace monitoring \
  --create-namespace \
  --values deploy/observability/kube-prometheus-stack-values.yaml \
  --wait \
  --debug
```

The values deliberately use an empty ServiceMonitor object selector inside a namespace allowlist. This preserves the monitors created by kube-prometheus-stack itself while also discovering Foretoken and DCGM monitors. It does not discover arbitrary ServiceMonitors from every namespace.

The namespace allowlist is part of the discovery trust boundary: anyone allowed to create a ServiceMonitor in one of those namespaces can influence which endpoints Prometheus scrapes. Restrict write access to them and copy the values file when the platform uses different namespace names.

Prometheus must reach dynamic Foretoken model-server Services across workload namespaces. Label only the namespace that actually runs the trusted Prometheus Pods:

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite
```

This label is not needed for DCGM. It allows every Pod in the labeled namespace to reach the shared model-server port because Kubernetes NetworkPolicy works at L3/L4 rather than per HTTP path. Do not add the label to `gpu-monitoring`.

## 2. Install DCGM Exporter

Install Prometheus Operator first so that the `ServiceMonitor` CRD exists, then add the official exporter repository:

```bash
helm repo add gpu-helm-charts \
  https://nvidia.github.io/dcgm-exporter/helm-charts
helm repo update
```

Create the isolated namespace and its ingress policy before the exporter Pod exists:

```bash
kubectl create namespace gpu-monitoring \
  --dry-run=client \
  --output yaml \
  | kubectl apply --filename -

kubectl apply \
  --filename deploy/observability/dcgm-exporter-network-policy.yaml
```

Choose one of the following install commands.

### Standard Kubernetes GPU node

```bash
helm upgrade --install dcgm-exporter \
  gpu-helm-charts/dcgm-exporter \
  --version 4.8.2 \
  --namespace gpu-monitoring \
  --create-namespace \
  --values deploy/observability/dcgm-exporter-values.yaml \
  --wait \
  --debug
```

### k3d with selected host GPUs

k3d runs a second NVIDIA container runtime inside the privileged node container. The pinned image defaults to `NVIDIA_VISIBLE_DEVICES=all`; without an override, that inner runtime may try to mount every host GPU it can enumerate, including devices not intended for this development cluster. Use the same trusted `GPU_INDICES` selection that configured the device plugin:

```bash
export GPU_INDICES=6,7

helm upgrade --install dcgm-exporter \
  gpu-helm-charts/dcgm-exporter \
  --version 4.8.2 \
  --namespace gpu-monitoring \
  --create-namespace \
  --values deploy/observability/dcgm-exporter-values.yaml \
  --set-json \
    "extraEnv=[{\"name\":\"NVIDIA_VISIBLE_DEVICES\",\"value\":\"${GPU_INDICES}\"}]" \
  --wait \
  --debug
```

This is an environment override, not a repository default. The host indices can become `gpu="0"` and `gpu="1"` inside k3d; use the `UUID` label, which the dashboard shows in per-GPU legends, to correlate them with `nvidia-smi`.

The override makes the exporter monitor the intended GPU set without requesting or consuming a Kubernetes `nvidia.com/gpu` allocation. In the validated two-GPU k3d environment, Prometheus received exactly two GPU UUIDs while Kubernetes advertised two schedulable GPUs.

This does not turn privileged nested k3d into a tenant-isolation boundary: the node can still enumerate other host GPUs on some runtimes, and a trusted process can override environment-based selection. Use this path for controlled development only, never set the exporter to `all` on a shared host, and use conventional node or VM isolation for untrusted workloads.

### Mixed CPU and GPU nodes

The upstream chart creates a DaemonSet and has no node selector by default. Before using these values in a mixed cluster, copy the file and select only GPU nodes using a label that the platform actually maintains, for example:

```yaml
nodeSelector:
  nvidia.com/gpu.present: "true"
```

GPU Feature Discovery commonly manages that label, but verify it with `kubectl get nodes --show-labels`; do not add a selector for a label that is absent or inaccurate.

### Non-default NVIDIA runtime

Do not set a runtime class merely because the cluster has NVIDIA GPUs. If the cluster requires an explicit RuntimeClass and `kubectl get runtimeclass nvidia` succeeds, use an environment-specific values overlay:

```yaml
runtimeClassName: nvidia
```

The repository values omit it because clusters without that RuntimeClass would leave the DaemonSet unschedulable.

## 3. Provision the Grafana dashboard

The dashboard is stored as canonical JSON and wrapped in a ConfigMap by Kustomize:

```bash
kubectl --namespace monitoring apply \
  --kustomize deploy/observability/grafana
```

The kube-prometheus-stack Grafana sidecar discovers the `grafana_dashboard=1` label and loads the dashboard named `Foretoken GPU 运行状态`. The dashboard selects a Prometheus datasource dynamically and lets the reader filter by scrape instance and exporter-visible GPU index. Per-GPU legends retain the device UUID so that an index can be reconciled with the physical device.

If the platform Grafana does not run the standard dashboard sidecar, import `deploy/observability/grafana/dashboards/foretoken-gpu-dashboard.json` through Grafana instead.

## 4. Enable Foretoken metrics

GPU monitoring works independently of Foretoken request routing. To collect Frontend and model-server metrics as well, include the following when installing or upgrading Foretoken:

```yaml
observability:
  serviceMonitor:
    enabled: true
```

The two Foretoken ServiceMonitors live in `foretoken-platform` but discover matching workload Services across the cluster. The DCGM ServiceMonitor lives in `gpu-monitoring` and selects only the exporter Service in that namespace.

## Verify each layer

Verify the path from the producer toward the dashboard. This order identifies the broken boundary instead of treating an empty panel as the root cause.

### 1. Exporter Pod and Service

```bash
kubectl --namespace gpu-monitoring rollout status \
  daemonset/dcgm-exporter \
  --timeout=3m

kubectl --namespace gpu-monitoring get \
  daemonset,pods,service,servicemonitor
```

The desired and ready DaemonSet counts should match the selected GPU-node count. The exporter resources should contain CPU and memory requests but no `nvidia.com/gpu` request or limit:

```bash
kubectl --namespace gpu-monitoring get daemonset dcgm-exporter \
  --output yaml
```

### 2. Raw OpenMetrics output

Bind port-forwarding to loopback only:

```bash
kubectl --namespace gpu-monitoring port-forward \
  --address 127.0.0.1 \
  service/dcgm-exporter \
  9400:9400
```

In another terminal:

```bash
curl --fail --silent http://127.0.0.1:9400/metrics \
  | grep 'DCGM_FI_DEV_GPU_TEMP'
```

One series per exporter-visible GPU confirms that DCGM can read the device. Stop the port-forward after verification.

### 3. Prometheus discovery

In Prometheus, confirm that the DCGM target is `UP`, then evaluate:

```promql
min(up{foretoken_observability_source="dcgm"})
```

The result should be `1`. Count unique exporter-visible GPUs with:

```promql
count(
  group by (instance, gpu, UUID) (
    DCGM_FI_DEV_GPU_TEMP{foretoken_observability_source="dcgm"}
    or DCGM_FI_DEV_GPU_UTIL{foretoken_observability_source="dcgm"}
    or DCGM_FI_DEV_FB_USED{foretoken_observability_source="dcgm"}
  )
)
```

In a two-GPU k3d node, the expected result is `2`. This count is independent of Kubernetes allocatable GPU count: DCGM reports devices visible to the exporter, while the device plugin advertises resources available for scheduling.

### 4. Grafana

```bash
kubectl --namespace monitoring get configmap \
  foretoken-gpu-dashboard

kubectl --namespace monitoring port-forward \
  --address 127.0.0.1 \
  service/kube-prometheus-stack-grafana \
  3000:80
```

Open `http://127.0.0.1:3000` and search for `Foretoken GPU 运行状态`. The dashboard text remains Chinese regardless of the Grafana account's interface language. Stop the port-forward when finished.

## Metric and dashboard semantics

The custom collector is a curated base-hardware set, not an inventory of every DCGM field. It includes:

| Area | Representative fields |
| --- | --- |
| Utilization | GPU, memory-copy, encoder, and decoder utilization |
| Memory | Framebuffer used and free |
| Thermal and power | GPU/memory temperature, current power, total energy |
| Clocks | SM and memory clocks |
| Throttling | Power, thermal, board, reliability, and related violation counters |
| PCIe and NVLink | Replay, bandwidth, CRC, replay, and recovery counters |
| Reliability | ECC totals, retired pages, remapped rows, and XID |

Configured does not mean emitted. A field may be unavailable for a particular GPU, driver, DCGM, or exporter combination. In the validated A100 environment, 37 fields were configured and 31 produced series; PCIe TX/RX throughput, XID, and three retired-page fields were not reported. The exporter did not log a field-specific cause, so absence must be displayed as **unavailable or unreported**, never converted to zero.

The dashboard follows the same rule:

- current-value cards use instant queries, so an old sample from the visible time range is not presented as the current state;
- gaps remain gaps and are not filled with zero;
- missing optional series remain Grafana `No data` rather than becoming zero; `未报告` is only a fallback when a returned field has a null value;
- `min(up{...})` reports an error when any selected exporter target is down;
- PCIe, NVLink, and throttling increases use a fixed five-minute window; and
- per-GPU series retain `(instance, gpu, UUID)` when they are reduced or aggregated.

Dashboard colors are operational hints, not alert rules. GPU inventory uses a neutral color because the expected device count is deployment-specific. Temperature and PCIe replay thresholds are generic visual defaults and must be calibrated for the deployed GPU model, platform policy, and workload before they are used for alerting.

The `gpu` label is the index visible inside the exporter node. Container and k3d GPU filtering can renumber devices, so `gpu="0"` does not necessarily mean physical host GPU 0. Use the `UUID` label when correlating a sample with `nvidia-smi`.

This baseline does not enable Pod attribution, MIG-specific panels, or vGPU panels. Treat their absence as unsupported by this dashboard rather than as proof that the platform has no MIG or vGPU configuration.

## Troubleshooting

### Exporter Pod is Pending

- Check node selectors, taints, and tolerations.
- Check whether an explicitly configured RuntimeClass exists.
- On k3d, verify that the node container was created with the intended GPUs and NVIDIA runtime mounts.

### Exporter Pod is running but hardware metrics are absent

- Read `kubectl --namespace gpu-monitoring logs daemonset/dcgm-exporter`.
- Verify the driver with `nvidia-smi` on the host and inside an NVIDIA runtime container.
- Confirm that the exporter sees the intended device files.
- With the repository values, logs should say that the NVML provider was skipped and should not contain repeated pod-resources socket warnings. If they do, inspect the rendered container arguments and Helm values merge: the running release is not using this baseline. A reused or older exporter with Kubernetes mapping enabled can emit such warnings; they affect Pod attribution and do not by themselves prove that base hardware telemetry failed.

### ServiceMonitor is absent

- Install Prometheus Operator and its CRDs before DCGM Exporter.
- Confirm that `serviceMonitor.enabled=true` reached the Helm release.
- Render the pinned chart with the repository values when diagnosing a values merge.

### Prometheus target is missing or down

- Confirm that the ServiceMonitor object is in an allowed namespace.
- Check its Service selector, named `metrics` port, endpoint, and namespace selector.
- Check the `dcgm-exporter-metrics` NetworkPolicy and the real Prometheus namespace. Kubernetes NetworkPolicies are additive, so another policy selecting the same Pod may allow additional sources.
- If the monitoring stack uses another namespace, copy both the Prometheus values and NetworkPolicy and update that namespace consistently.

### Dashboard is present but empty

- Confirm the Prometheus query returns `up{foretoken_observability_source="dcgm"}`.
- Select the correct Prometheus datasource and reset the instance/GPU filters to `All`.
- Confirm the dashboard ConfigMap is in the namespace watched by the Grafana sidecar.
- Remember that unsupported fields remain empty; start with GPU temperature or utilization.

### Duplicate GPU series

Do not run standalone DCGM Exporter alongside a GPU Operator-managed exporter on the same nodes. If one exporter is scraped by multiple ServiceMonitors, remove the duplicate discovery path rather than hiding it only in Grafana.

## Remove or roll back

The components have independent ownership. Remove only resources installed for the development cluster:

```bash
helm uninstall dcgm-exporter \
  --namespace gpu-monitoring

kubectl delete \
  --filename deploy/observability/dcgm-exporter-network-policy.yaml

kubectl --namespace monitoring delete \
  --kustomize deploy/observability/grafana
```

Do not uninstall a shared GPU Operator, Prometheus stack, or Prometheus Operator CRDs as part of Foretoken cleanup. Delete the kube-prometheus-stack release only when it is dedicated to the disposable development cluster and no other workloads depend on it.

## Upstream references

- [Install DCGM Exporter](https://docs.nvidia.com/datacenter/dcgm/latest/installation/install-dcgm-exporter.html)
- [DCGM Exporter metric fields, units, and labels](https://docs.nvidia.com/datacenter/dcgm/latest/reference/dcgm-exporter-metrics.html)
- [NVIDIA DCGM Exporter `4.5.3-4.8.2` Helm chart values](https://github.com/NVIDIA/dcgm-exporter/blob/4.5.3-4.8.2/deployment/values.yaml)
- [Grafana dashboard variables](https://grafana.com/docs/grafana/latest/dashboards/variables/)
