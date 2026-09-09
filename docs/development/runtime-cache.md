<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Persistent Runtime Cache

English | [简体中文](runtime-cache_zh.md)

## Reuse one data directory

The current-source examples keep downloaded models, tokenizer files, and engine caches under `./data`. This directory mode requires the CLI and controller built from the same source; it is not available in the published 0.0.2 package.

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: RuntimeCache
metadata:
  name: models
spec:
  directory: ./data
  accessMode: ReadWriteMany
```

`directory` replaces `initialSize`, `maxSize`, and `storageClassName`. It does not impose a capacity limit: available space and any quota belong to the backing filesystem. The static PV and PVC use an internal binding request because Kubernetes requires one, not because Foretoken reserves or limits that amount of disk space.

### Local k3d

Before creating the cluster, prepare the example directory and make it writable by the workload's user (the standard images use UID/GID 65532). Mount it into the nodes that will run the frontend and models:

```bash
mkdir -p examples/quickstart/data
k3d cluster create "$CLUSTER" \
  --config deploy/k3d/config.yaml \
  --volume "$PWD/examples/quickstart/data:/var/lib/foretoken/data@all"
```

Keep the GPU options from the [k3d guide](../k3d-deployment.md) when creating GPU nodes. Then install the current source and deploy:

```bash
pip install -e .
foretoken install -e .
foretoken deploy examples/quickstart
```

The CLI resolves `./data` against the Kustomize root, not the caller's working directory. It verifies a writable Docker bind mount, including a mount of a parent such as the repository root, and restricts the PV to nodes exposing that directory. Mount destinations must match across those nodes. A remote Docker endpoint is not a client-local filesystem and is rejected for this path. Recreating a k3d cluster requires restoring its bind mount; the files stay in the host directory.

### Other Kubernetes clusters

Use an absolute directory path that administrators have already prepared on the target nodes:

```yaml
spec:
  directory: /srv/foretoken/data
  accessMode: ReadWriteMany
```

On a single-node cluster, Foretoken pins the PV to that node. On a multi-node cluster, this declaration means the same shared filesystem is already mounted at this exact path on **every** node, with permissions for the workloads. Equal path strings alone do not establish sharing. Use a shared filesystem or a normal storage-backed PVC when that condition does not hold. The CLI neither uploads client files nor installs a shared filesystem, and does not verify remote filesystem identity.

The deploy identity needs permission to read nodes and create/read static PersistentVolumes, in addition to its ordinary namespace permissions. The controller still owns the namespaced PVC. Direct `kubectl apply` of the RuntimeCache alone does not prepare the PV; use `foretoken deploy` for directory declarations. Cluster admission policies may disallow hostPath volumes; use dynamic storage in those clusters rather than weakening admission policy.

## Models within the directory

For an empty directory, keep a Hub model ID such as `Qwen/Qwen3-0.6B`. The inference engine and frontend download into their usual cache layout below the mount. Later Pods reuse those files; an upstream revision check may still access the Hub.

To use a prepared checkpoint, place its complete model directory below `data`:

```text
examples/quickstart/data/checkpointA/A3/
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── model.safetensors
```

Set `ModelService.spec.model` to `checkpointA/A3`. Both the model-server and frontend resolve it under the mounted data root, while requests continue to use `checkpointA/A3` as the model ID. Local model directories may contain the formats supported by the inference engine; format and tokenizer validation remain with the engine. A separate tokenizer directory does not need model weights. An existing individual checkpoint file is rejected, never silently replaced with its parent directory. Relative paths and symlinks cannot escape the data root. Explicit absolute model directories retain their existing Pod-local meaning.

## Dynamic PVC storage

Remove `directory` and choose a StorageClass when the cluster should provision the volume:

```yaml
spec:
  initialSize: 10Gi
  storageClassName: shared-storage
  accessMode: ReadWriteMany
```

Omit `storageClassName` to use the cluster default. `initialSize` is the actual requested capacity; many storage drivers provision and enforce that size. K3s `local-path` does not enforce a directory capacity limit, so its usable space depends on the node filesystem. Do not assume this behavior for CSI block or network volumes.

Set `maxSize` only when the selected driver supports online volume and filesystem expansion. Foretoken requests growth when the lowest observed free-space ratio reaches 20%, doubling the request up to `maxSize`. The maximum can be increased but not removed or decreased; PVC shrinking is not supported. An in-progress download can still exhaust space before expansion finishes.

## Retention and recovery

Both modes default to `retentionPolicy: Retain`. Deleting a directory-mode deployment preserves its files and PV; deleting a Namespace still deletes PVC objects within it. Re-deploying the same namespace/cache name reuses the unchanged directory binding, including a retained PV whose old PVC was deleted. The CLI refuses an existing PV with another owner, directory, or node placement. Use a new cache name for a different directory rather than repointing an existing claim.

Explicit `retentionPolicy: Delete` removes the directory-mode PVC and CLI-created PV objects after workloads terminate, but still leaves the directory contents untouched. Remove files separately only when they are no longer needed. Dynamic PVC deletion follows its StorageClass reclaim policy and may delete the backing storage.

If persistent storage becomes unwritable during model-server startup, Foretoken tears down the failed EngineCore and retries with Pod-scoped temporary caches. A missing frontend Hub snapshot can likewise download into its temporary volume. This does not migrate preloaded local checkpoints to temporary storage or make an unmounted PVC usable. Temporary caches disappear with the Pod.

Administrators may instead set `workload.cache.claimName` in platform values to mount an existing PVC. Foretoken does not create, expand, or delete that claim; every workload namespace using those values must supply it.
