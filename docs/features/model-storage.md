<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Model storage

[中文](model-storage_zh.md)

The Quick Start examples share downloaded models and runtime caches in the repository-root `data/` directory. Their `cache.yaml` files use:

```yaml
spec:
  directory: ../../../data
  accessMode: ReadWriteMany
```

Then deploy as usual:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

Foretoken creates the directory-backed volume for this configuration. The directory is retained when the service is deleted, so later deployments can reuse its contents. See [Model sources](model-sources.md) to select ModelScope or a Hugging Face-compatible endpoint.

## Choose where the directory lives

For local k3d, relative paths resolve from the Kustomize root. Both examples use `../../../data` to reach the repository-root directory; bind that directory into the nodes when creating the cluster. See the [k3d guide](../deployment/k3d.md).

Existing deployments can keep their current path. Changing the directory of an already bound cache requires a new RuntimeCache name; move or copy existing files separately if needed.

For a remote cluster, the deploy command automatically converts a relative directory such as `../../../data` into a dynamic PVC, so the Quick Start YAML does not need to change. Multi-node clusters keep `ReadWriteMany` for shared model and runtime data; a single-node cluster uses `ReadWriteOnce` automatically. To select a particular StorageClass, size, access mode, or maximum size, set those fields in `cache.yaml`; an absolute `directory` remains a host path that must already exist on the target node or shared filesystem.

## Use an existing model

Place a complete model directory below the data root:

```text
data/models/checkpointA/A3/
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── model.safetensors
```

Set the public model identifier in `model.yaml`:

```yaml
spec:
  model: checkpointA/A3
  source: local
```

The model server and frontend resolve this identifier below `data/models`. A tokenizer-only directory may be placed in `data/models/tokenizers`; model format validation remains with the inference engine.

## Use dynamic storage

Remove `directory` when the cluster should provision a PVC:

```yaml
spec:
  initialSize: 10Gi
  accessMode: ReadWriteMany
```

Add `storageClassName` to select a StorageClass. Add `maxSize` only when its driver supports online expansion.

To mount a PVC that is managed elsewhere, set `workload.cache.claimName` in platform values and create that claim in each workload namespace.

Diagnostic captures use the same data root and store results below `profiles/`; see [Profiling](../../benchmarks/docs/profile/README.md).

## Distribute models across nodes

`foretoken install` automatically prepares or reuses shared model downloads. Models with persistent storage reuse cached files first. Missing files from anonymously accessible models on the official Hugging Face and ModelScope services can be downloaded through node peers. Authenticated downloads and custom model endpoints use their provider directly.

### Load weights from running replicas

On NVIDIA clusters with RDMA, ModelExpress can also load weights from running replicas. To enable this optional optimization, save the following in `deploy/platform-values.yaml`:

```yaml
modelDistribution:
  modelexpress:
    enabled: true
```

Weight transfer applies to remote models with a persistent cache, data parallelism of one, and fixed expert placement. An explicit `load-format` remains unchanged; replicas without a compatible source load the prepared files. Set `modelexpress.enabled` to `false` to disable weight transfer.

### Customize shared downloads

The installer manages Dragonfly for shared downloads. A namespace's `pod-security.kubernetes.io/enforce` label disables node-peer downloads when set to `baseline` or `restricted`.

To use direct downloads throughout the platform, or when other cluster policies prohibit `hostPath` mounts, set:

```yaml
modelDistribution:
  dragonfly:
    enabled: false
```

Set `enabled: true` to re-enable shared downloads. If several Dragonfly installations exist, select one by setting `modelDistribution.dragonfly.existingRelease` to `{name: dragonfly, namespace: dragonfly-system}`, using the existing release's name and namespace.

Apply custom settings with the original installation command, adding `--values deploy/platform-values.yaml`. Source installations retain `-e .`, registry settings, and any engine-source bindings. For a published platform:

```bash
foretoken install --values deploy/platform-values.yaml
```

Redeploy the affected services after changing these settings. `foretoken uninstall` removes managed Dragonfly resources once no workloads use them; reused installations are retained.
