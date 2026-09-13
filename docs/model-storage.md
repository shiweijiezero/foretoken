<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Model storage

[中文](model-storage_zh.md)

Keep downloaded models and runtime caches in the example data directory:

```yaml
spec:
  directory: ./data
  accessMode: ReadWriteMany
```

Then deploy as usual:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

Foretoken creates the directory-backed volume for this configuration. The directory is retained when the service is deleted, so later deployments can reuse its contents.

## Choose where the directory lives

For local k3d, bind the example's `data` directory into the nodes before creating the cluster. See the [k3d guide](k3d-deployment.md).

For a remote cluster, use an absolute path already available on the target node or on the same shared filesystem at every target node. A client-local `./data` directory is not uploaded automatically.

## Use an existing model

Place a complete model directory below the data root:

```text
examples/quickstart/data/models/checkpointA/A3/
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── model.safetensors
```

Set the public model identifier in `model.yaml`:

```yaml
spec:
  model: checkpointA/A3
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

Diagnostic captures use the same data root and store results below `profiles/`; see [Profiling](../observability/profiling.md).
