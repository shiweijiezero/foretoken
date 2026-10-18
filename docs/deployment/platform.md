<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Configure the Foretoken platform

English | [简体中文](platform_zh.md)

Use `foretoken install` to configure frontend access, image sources, hardware, and model distribution. For the default installation and command overview, see the [CLI guide](../../cli/README.md#install-the-kubernetes-platform).

Run the examples from the Foretoken repository root with the target Kubernetes context selected. The commands below use a published platform. For a source installation, retain `-e .`, your registry settings, and any `--engine-source` bindings when reapplying settings. Redeploy affected services to use updated platform defaults.

## Gateway access

The default frontend access mode uses a `LoadBalancer` Service. To route requests through a Gateway and hostname, add the public hostname under `spec` in `examples/quickstart/qwen3-0.6b/frontend.yaml`:

```yaml
spec:
  hostname: foretoken.example.com
```

Enable Gateway mode:

```bash
foretoken install --frontend-mode gateway
```

This creates a dedicated `GatewayClass` and `Gateway`, installing Envoy Gateway when no compatible controller is available. Deploy the service and send a request as shown in the [Gateway example](../../README.md#gateway-mode).

To use an existing Gateway, supply its name and namespace. This example uses `inference-gateway` in `gateway-system`; replace both with your Gateway's values:

```bash
foretoken install \
  --frontend-mode gateway \
  --gateway-name inference-gateway \
  --gateway-namespace gateway-system
```

With multiple listeners, add `--gateway-section-name` followed by the listener name. A Gateway managed by another controller keeps that controller's configuration and lifecycle.

## Image and Chart sources

Without an override, installation selects public sources for default platform images and Helm Chart downloads. To select a source explicitly, pass its registry prefix with `--oci-registry`, or set `FORETOKEN_OCI_REGISTRY`. Image references explicitly supplied through values remain unchanged.

Source selection runs on the CLI host; the selected registry must also be reachable from cluster nodes. Chart downloads and image pulls during source builds can fall back to the original source when an automatically selected mirror fails.

`--registry` has a different purpose: it distributes source-built images to a remote cluster and requires `-e`. Registry setup and authentication are covered by [Kubernetes deployment](kubernetes.md#2-install-foretoken-from-source).

## Custom platform values

Use a values file when changing platform settings. Save the relevant settings below in `deploy/platform-values.yaml`, then apply them:

```bash
foretoken install --values deploy/platform-values.yaml
```

`--values` can be repeated to apply multiple files. The [Chart values](../../deploy/charts/foretoken/values.yaml) list the available settings. Monitoring, Grafana login, and persistent logs are covered by [Observability](../../observability/README.md#platform-settings).

### LoadBalancer addresses

Model endpoints need an address reachable from outside the cluster. k3d, K3s, and cloud clusters normally provide address allocation. Clusters built with kubeadm, RKE2, or Kubespray may need a LoadBalancer implementation; installation reports `LoadBalancer support Not verified` when it cannot find one.

To let Foretoken manage address allocation through MetalLB, obtain an unused range in the nodes' subnet from the cluster administrator. Replace this example range with those addresses:

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

### Hardware selection

Installation automatically selects the NVIDIA or MetaX runtime and reuses or installs LeaderWorkerSet and shared RDMA device allocation. GPU nodes still need their vendor driver and Kubernetes device plugin.

Explicit values take precedence. In a mixed-GPU cluster, use `runtime.vllm.gpu.resourceName` to select the GPU resource, or `runtime.vllm.gpu.nodeSelector` to limit the nodes considered. For MetaX setup, see [Prepare Foretoken for MetaX GPUs](metax-platform.md).

### Share model downloads with Dragonfly

Dragonfly shares public model downloads between nodes:

```yaml
modelDistribution:
  dragonfly:
    enabled: true
```

Installation prepares Dragonfly or reuses an existing installation. Models requiring authentication and custom model endpoints download directly from their provider. To select a particular Dragonfly Helm release, set `existingRelease: {name: dragonfly, namespace: dragonfly-system}` under `modelDistribution.dragonfly`, using your release's name and namespace.

### Transfer loaded weights with ModelExpress

On NVIDIA clusters with RDMA, ModelExpress can load weights from running replicas:

```yaml
modelDistribution:
  modelexpress:
    enabled: true
```

If Dragonfly is also enabled, put both settings under the same `modelDistribution` mapping. Automatic weight transfer applies to remote models with a persistent cache, data parallelism of one, and fixed expert placement. An explicit `load-format` remains unchanged; replicas without a compatible source load the prepared files.

After changing either distribution setting, reapply the values and redeploy the affected services. Set the corresponding `enabled` field to `false` to disable it. `foretoken uninstall` removes managed Dragonfly resources once no workloads use them; reused installations are retained.
