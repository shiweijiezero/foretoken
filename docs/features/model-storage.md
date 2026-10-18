<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Model storage

[中文](model-storage_zh.md)

Use `cache.yaml` to choose the data directory for a deployment. The Quick Start selects the repository's `data/` directory:

```yaml
spec:
  directory: ../../../data
  accessMode: ReadWriteMany
```

Deploy normally:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

Deployments that mount the same directory reuse downloaded model files, including weights, tokenizers and model configuration. Runtime state is kept separately for each namespace:

| Directory | Contents |
|---|---|
| `data/shared/models/` | Managed model downloads shared by services |
| `data/namespaces/<namespace>/` | Compilation caches, runtime configuration, account state and profiling captures |
| `data/models/` | Model files supplied and maintained outside Foretoken's download cache |

Source-build caches are managed separately from model storage. See [Model sources](model-sources.md) to select ModelScope or a Hugging Face-compatible endpoint.

## Choose a directory

Relative paths resolve from the Kustomize directory. In a Git worktree, a relative path to the repository's default `data/` uses the primary checkout's `data/`, so candidates can reuse downloads without copying model files. Explicit absolute paths keep their selected location.

For local k3d, the CLI prepares the repository data mount when creating the cluster. See the [k3d guide](../deployment/k3d.md). On a remote cluster, the selected directory must be available on the nodes that run its consumers; use a shared filesystem for multi-node access. A directory remains a directory selection rather than being replaced with an unrelated dynamically provisioned volume.

Changing the directory of an already bound RuntimeCache requires a new RuntimeCache name. Move or copy user-maintained model files separately when changing their location.

## Share model files across namespaces

For member namespaces, an administrator prepares access to the shared directory once. If the repository's `data/` is on an NFS mount visible to the CLI host and the cluster, run these commands from the repository root:

```bash
foretoken install --namespace candidate-a
foretoken install --namespace candidate-b
```

Local k3d uses its existing data bind mount instead. Namespace installation prepares a claim in each namespace for the same underlying directory. Members keep selecting that directory in `cache.yaml` and use the normal deployment commands; they do not need PVC names or cluster-wide storage permissions. Assign [namespace service permissions](../../cli/README.md#prepare-a-namespace-for-members) before handing the namespace to members.

When the CLI host cannot inspect the shared mount, the administrator can bind a directory selection to an existing NFS-backed `ReadWriteMany` claim. For example, save the following in `deploy/member-storage.yaml`, replacing the directory and source claim with the intended storage:

```yaml
namespaceAccess:
  directories:
    models:
      directory: /srv/shared/foretoken-data
      sourceClaim: model-library/models
```

Then install both namespaces with that selection:

```bash
foretoken install --namespace candidate-a --values deploy/member-storage.yaml
foretoken install --namespace candidate-b --values deploy/member-storage.yaml
```

Members select `/srv/shared/foretoken-data` as `spec.directory`. Native NFS and `nfs.csi.k8s.io` source claims are supported. A local filesystem on the CLI host alone does not provide shared remote storage; use a node-accessible directory or choose dynamic storage below.

## Stop, delete and reuse

`foretoken stop` releases serving resources while retaining the service's model-cache references. Resume with `foretoken deploy` to reuse those files.

`foretoken delete` releases the deleted services' references after their workloads exit. Foretoken collects each managed model version when its last reference in that directory is gone. For example, if namespaces A and B use model X while C and D use model Y, deleting A and B can reclaim X without waiting for Y to stop.
The directory itself, user-supplied files in `data/models/`, and namespace-local state are retained. Administrator-prepared claims remain available for later deployments. Dynamic volumes follow their configured retention and StorageClass reclaim policies.

## Use an existing model

Place a complete model directory below the data root:

```text
data/models/checkpointA/A3/
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── model.safetensors
```

Select it in `model.yaml`:

```yaml
spec:
  model: checkpointA/A3
  source: local
```

The model server and frontend resolve this identifier below `data/models`. A tokenizer-only directory may be placed in `data/models/tokenizers`; the inference engine validates the model format. These user-maintained files are not collected with downloaded model versions. Keep local model directories outside `data/shared/models`; select `hf` or `modelscope` to use managed downloads.

## Use dynamic storage

Remove `directory` when the cluster should provision storage instead of using a selected filesystem directory:

```yaml
spec:
  initialSize: 10Gi
  accessMode: ReadWriteMany
```

Use `ReadWriteOnce` for a single-node volume when that is what the StorageClass supports. Add `storageClassName` to select a StorageClass, and `maxSize` only when its driver supports online expansion. Separate dynamically provisioned claims do not share files merely because they use the same StorageClass.

An administrator can set `workload.cache.claimName` to use an existing namespace-local PVC as the default. An explicit authorized `directory` selection takes precedence over that default.

Profiling results stay in the namespace's data area; use [Profiling](../../benchmarks/docs/profile/README.md) to collect and view them.
