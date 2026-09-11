<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Model storage

English | [简体中文](model-storage_zh.md)

Keep model downloads and runtime caches in one directory so new Pods can reuse them. Directory caching uses the [current-source CLI and platform](custom-deployment.md).

## Use a data directory

The source examples declare the data directory in `cache.yaml`:

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: RuntimeCache
metadata:
  name: models
spec:
  directory: ./data
  accessMode: ReadWriteMany
```

For local k3d, `./data` is relative to the example's Kustomize directory. Create it before the cluster and bind it into the nodes that run the model and frontend. The [k3d guide](k3d-deployment.md) includes these mounts. The directory must be writable by the Pod users; the standard frontend uses UID/GID 65532.

For other Kubernetes clusters, use an absolute node path:

```yaml
spec:
  directory: /srv/foretoken/data
  accessMode: ReadWriteMany
```

On a single-node cluster this is a local directory. On a multi-node cluster, prepare the same shared filesystem at this path on every node. The command uses the existing directory; it does not upload files from the CLI machine. Deploying this mode requires permission to read nodes and create static PersistentVolumes.

Directory capacity is determined by its filesystem and quotas. Do not set `initialSize`, `maxSize`, or `storageClassName` with `directory`.

After preparing storage, deploy from the repository root:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

An empty `data` directory is sufficient: models download into the cache on first use. Re-deploying uses the existing files, although the model provider may check for updates. Frontend and model-server use the same data directory.

## Load a local model

Place a complete model directory below `data`, for example:

```text
examples/quickstart/data/checkpointA/A3/
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── model.safetensors
```

In `model.yaml`, set:

```yaml
spec:
  model: checkpointA/A3
```

Use `checkpointA/A3` as the model name in API requests. File formats and required tokenizer files follow the inference engine's model loader. A single checkpoint file must first be packaged as a supported model directory. Relative paths and symlinks stay within `data`; absolute model paths refer to directories inside the Pod.

## Use a StorageClass

To let Kubernetes provision storage, replace `directory` with a capacity in `cache.yaml`:

```yaml
spec:
  initialSize: 10Gi
  accessMode: ReadWriteMany
```

This uses the default StorageClass. Add `storageClassName` to select another one. A multi-node deployment needs storage that supports `ReadWriteMany`.

If the driver supports online expansion, add `maxSize`, such as `100Gi`. It can be increased later, but not removed or decreased. Storage drivers may enforce the requested volume size; K3s `local-path` instead uses the available space of its backing filesystem.

## Keep or remove data

Directory mode retains files when services or Pods are deleted. Keep `data` when recreating a k3d cluster and restore its bind mount. Re-deploying the same example reuses the retained storage.

`retentionPolicy: Delete` removes the directory-mode PV/PVC objects, not the files. Remove those files separately when no longer needed. For dynamic PVCs, the StorageClass reclaim policy determines whether deleting the claim also deletes the underlying data.
