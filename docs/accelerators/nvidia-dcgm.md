<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# NVIDIA DCGM accelerator adapter

English | [简体中文](nvidia-dcgm_zh.md)

This guide connects NVIDIA GPU hardware telemetry to the Foretoken accelerator observability boundary. It installs or reuses NVIDIA DCGM Exporter, makes the exporter discoverable by Prometheus, and loads a Chinese Grafana dashboard.

Read [Accelerator observability](../accelerator-observability.md) first for the cross-vendor boundary. DCGM is the NVIDIA adapter; its `DCGM_FI_*` metric names and `UUID` device label are not a common schema for other vendors.

## Choose a deployment path

| Cluster state | Path |
| --- | --- |
| GPU Operator or DCGM Exporter already runs on the selected nodes | [Reuse the existing exporter](#reuse-an-existing-exporter) |
| Standard Kubernetes GPU nodes without an exporter | [Install the standalone adapter](#install-the-standalone-adapter) |
| Foretoken k3d development cluster | Install the standalone adapter, then apply the [k3d device overlay](#select-devices-in-k3d) |

Do not run two DCGM Exporter DaemonSets on the same nodes. Duplicate exporters waste resources and can produce duplicate series.

## Prerequisites

Before installing the adapter, confirm that:

- NVIDIA drivers and the NVIDIA Container Toolkit work on each selected node;
- the container runtime can expose GPUs to containers;
- Prometheus Operator and Grafana are installed as described in [Observability](../../observability/README.md);
- the Prometheus namespace is labeled `inference.foretoken.io/metrics-scraper=true`;
- you know whether another platform component already provides DCGM Exporter.

For k3d, first complete [Deploy Foretoken with k3d](../k3d-deployment.md) and verify the GPU capacity reported by Kubernetes.

Run the commands below from the Foretoken repository root.

## Install the standalone adapter

The repository provides one adapter-owned values file. A homogeneous NVIDIA GPU cluster whose default runtime exposes GPUs does not require editing it. A mixed-node or non-default-runtime cluster must apply the small override described below before installation. The base values keep DCGM Exporter non-root, remove `SYS_ADMIN`, do not reserve a schedulable `nvidia.com/gpu` resource, and use a non-profiling hardware collector.

Add the official chart repository:

```bash
helm repo add gpu-helm-charts \
  https://nvidia.github.io/dcgm-exporter/helm-charts
helm repo update
```

Create the adapter namespace and apply the ingress policy:

```bash
kubectl create namespace accelerator-monitoring \
  --dry-run=client \
  --output yaml \
  | kubectl apply --filename -

kubectl apply \
  --filename deploy/observability/accelerators/nvidia-dcgm/network-policy.yaml
```

Install the pinned chart:

```bash
helm upgrade --install dcgm-exporter \
  gpu-helm-charts/dcgm-exporter \
  --version 4.8.2 \
  --namespace accelerator-monitoring \
  --values deploy/observability/accelerators/nvidia-dcgm/values.yaml \
  --wait
```

Chart `4.8.2` pins the corresponding `4.5.3-4.8.2-distroless` image. Review upstream release notes and revalidate the rendered security context, collector, and dashboard before changing the chart version.

The upstream chart still renders a read-only `/var/lib/kubelet/pod-resources` hostPath even though `--kubernetes=false` disables Pod attribution at runtime. A cluster policy that forbids this hostPath can reject the DaemonSet. In that environment, reuse a platform-managed exporter or validate an upstream-compatible chart overlay; do not weaken Pod Security policy only for this add-on.

### Select devices in k3d

In standard Kubernetes, the DaemonSet observes the GPUs on each selected node. In a nested k3d development cluster, explicitly pass the host indices for the same physical GPU set selected when the cluster was created. Use device UUIDs to verify the mapping against the NVIDIA device plugin.

Copy the one-value overlay:

```bash
cp \
  deploy/observability/accelerators/nvidia-dcgm/k3d-values.example.yaml \
  /tmp/foretoken-nvidia-dcgm-k3d-values.yaml
```

Change only `extraEnv[0].value` in the copy. For a cluster created with `GPU_INDICES=6,7`, keep:

```yaml
extraEnv:
  - name: NVIDIA_VISIBLE_DEVICES
    value: "6,7"
```

Install or update the exporter with both values files:

```bash
helm upgrade --install dcgm-exporter \
  gpu-helm-charts/dcgm-exporter \
  --version 4.8.2 \
  --namespace accelerator-monitoring \
  --values deploy/observability/accelerators/nvidia-dcgm/values.yaml \
  --values /tmp/foretoken-nvidia-dcgm-k3d-values.yaml \
  --wait
```

This selects devices for a trusted development cluster; it is not a tenant-isolation boundary. A privileged k3d node can still enumerate additional host devices on some runtime configurations. Use conventional node or VM isolation for untrusted workloads.

### Optional cluster-specific overrides

Keep cluster-specific settings in a small second values file rather than modifying the adapter defaults:

| Situation | Override |
| --- | --- |
| Mixed CPU and GPU nodes | `nodeSelector` using a label maintained by the platform, such as `nvidia.com/gpu.present: "true"` |
| Cluster requires an explicit NVIDIA runtime | `runtimeClassName: nvidia`, after verifying that RuntimeClass exists |
| Tainted accelerator nodes | The minimum matching `tolerations` |

Do not add a RuntimeClass, node label, or toleration merely because it appears in an example. First inspect the target cluster.

## Reuse an existing exporter

When GPU Operator or another platform component already owns DCGM Exporter, do not install the standalone chart. Configure its ServiceMonitor to add the accelerator adapter labels:

```yaml
relabelings:
  - targetLabel: foretoken_observability_source
    replacement: accelerator
  - targetLabel: foretoken_accelerator_vendor
    replacement: nvidia
  - targetLabel: foretoken_accelerator_exporter
    replacement: dcgm
```

The ServiceMonitor namespace must be included in Prometheus `serviceMonitorNamespaceSelector`. The supplied kube-prometheus-stack values include `accelerator-monitoring`; copy that file and add the operator's namespace if it differs.

Before loading the dashboard, inspect a raw series and confirm that the required `DCGM_FI_*` families exist and each GPU series has an uppercase `UUID` label. Legacy DCGM namespace mode may use lowercase `uuid`; disable that mode or adapt the NVIDIA dashboard before reuse.

The supplied NetworkPolicy selects the labels rendered by the standalone chart. When reusing an operator-managed exporter, inspect its Pod labels and provide an equivalent policy if the operator does not already manage access.

## Load the Grafana dashboard

Apply the adapter dashboard ConfigMap to the namespace that runs Grafana:

```bash
kubectl apply \
  --namespace monitoring \
  --kustomize deploy/observability/accelerators/nvidia-dcgm/grafana
```

The kube-prometheus-stack sidecar discovers the `grafana_dashboard=1` label and loads `Foretoken NVIDIA GPU 运行状态`. Dashboard text remains Chinese regardless of the Grafana account's interface language.

Foretoken Frontend and model-server metrics are independent of this adapter. Enable their ServiceMonitors separately with `observability.serviceMonitor.enabled=true` as described in [Observability](../../observability/README.md).

## Verify the adapter

### 1. Exporter resources

```bash
kubectl rollout status \
  daemonset/dcgm-exporter \
  --namespace accelerator-monitoring \
  --timeout=5m

kubectl get daemonset,service,servicemonitor \
  --namespace accelerator-monitoring
```

The desired and ready DaemonSet counts should match the nodes the DaemonSet is intended to monitor. The base values select every node and therefore assume a homogeneous GPU cluster; confirm the `nodeSelector` before proceeding in a mixed cluster.

### 2. Native metrics

```bash
kubectl port-forward \
  --namespace accelerator-monitoring \
  service/dcgm-exporter \
  19400:9400
```

In another terminal:

```bash
curl --fail --silent http://127.0.0.1:19400/metrics \
  | grep -E '^DCGM_FI_DEV_(GPU_TEMP|GPU_UTIL|FB_USED)\{'
```

At least one series for each exporter-visible GPU confirms that DCGM can read the device. Stop the port-forward after the check.

### 3. Prometheus target and device inventory

The adapter target should be up:

```promql
min(up{
  foretoken_observability_source="accelerator",
  foretoken_accelerator_vendor="nvidia",
  foretoken_accelerator_exporter="dcgm"
})
```

The result should be `1`. Count exporter-visible devices with:

```promql
count(
  group by (instance, gpu, UUID) (
    DCGM_FI_DEV_GPU_TEMP{
      foretoken_observability_source="accelerator",
      foretoken_accelerator_vendor="nvidia",
      foretoken_accelerator_exporter="dcgm"
    }
    or DCGM_FI_DEV_GPU_UTIL{
      foretoken_observability_source="accelerator",
      foretoken_accelerator_vendor="nvidia",
      foretoken_accelerator_exporter="dcgm"
    }
    or DCGM_FI_DEV_FB_USED{
      foretoken_observability_source="accelerator",
      foretoken_accelerator_vendor="nvidia",
      foretoken_accelerator_exporter="dcgm"
    }
  )
)
```

Compare the UUIDs and count with `nvidia-smi -L` on the node. Exporter-visible device count and Kubernetes allocatable GPU count measure different boundaries and need not match unless the deployment intentionally selected the same set.

### 4. Grafana

```bash
kubectl port-forward \
  --namespace monitoring \
  service/kube-prometheus-stack-grafana \
  3000:80
```

For the recommended kube-prometheus-stack release, the username is `admin`. Read its generated password from the release Secret:

```bash
kubectl get secret kube-prometheus-stack-grafana \
  --namespace monitoring \
  --output jsonpath='{.data.admin-password}' \
  | base64 --decode
printf '\n'
```

Open `http://127.0.0.1:3000` and search for `Foretoken NVIDIA GPU 运行状态`. Do not place the generated password in documentation or source control.

## Metric coverage and semantics

The adapter keeps the complete configured native output. Its non-profiling collector includes:

| Category | Examples |
| --- | --- |
| Utilization | GPU, memory-copy, encoder, and decoder utilization |
| Memory | Framebuffer used/free and memory temperature |
| Thermal and power | GPU temperature, power, total energy, and throttling counters |
| Clocks and links | SM/memory clocks, PCIe, and NVLink fields |
| Reliability | XID, ECC, retired/remapped pages, and row-remap status |

The dashboard visualizes the currently recognized subset; Prometheus retains the other configured metric families. Unsupported fields may be absent. A missing series is not zero, and dashboard color thresholds are visual hints rather than alert policies.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Exporter Pod is not Ready | NVIDIA runtime, driver compatibility, node selection, and Pod logs |
| Prometheus target is down | ServiceMonitor labels, discovery namespace, trusted scraper namespace label, and NetworkPolicy |
| Target is up but no GPU series exist | Exporter-visible devices, DCGM initialization, and enabled collector fields |
| Dashboard shows no data | Prometheus datasource, all three adapter labels, native metric names, and uppercase `UUID` |
| Duplicate device series | More than one exporter or more than one ServiceMonitor scraping the same target |

Useful commands:

```bash
kubectl logs \
  --namespace accelerator-monitoring \
  daemonset/dcgm-exporter

kubectl get prometheus \
  --namespace monitoring \
  --output yaml
```

## Clean up

For a standalone installation, remove only the release and policy created by this guide:

```bash
helm uninstall dcgm-exporter \
  --namespace accelerator-monitoring

kubectl delete \
  --ignore-not-found \
  --filename deploy/observability/accelerators/nvidia-dcgm/network-policy.yaml

rm -f /tmp/foretoken-nvidia-dcgm-k3d-values.yaml
```

When reusing an existing exporter, do not run `helm uninstall`. Use its lifecycle owner to remove only the three relabelings or other integration changes that were added for Foretoken.

For either path, remove the Foretoken dashboard when it is no longer needed:

```bash
kubectl delete configmap foretoken-nvidia-gpu-dashboard \
  --namespace monitoring \
  --ignore-not-found
```

Delete `accelerator-monitoring` only when no other accelerator adapter uses it. Do not uninstall a shared GPU Operator, Prometheus stack, or Prometheus Operator CRD as part of Foretoken cleanup.

## References

- [NVIDIA DCGM Exporter installation](https://docs.nvidia.com/datacenter/dcgm/latest/installation/install-dcgm-exporter.html)
- [DCGM Exporter metric fields, units, and labels](https://docs.nvidia.com/datacenter/dcgm/latest/reference/dcgm-exporter-metrics.html)
- [NVIDIA DCGM Exporter chart values](https://github.com/NVIDIA/dcgm-exporter/blob/4.5.3-4.8.2/deployment/values.yaml)
