<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Model storage

[中文](model-storage_zh.md)

The Quick Start uses the platform's prepared default storage. If none is configured, [request a volume](#use-dynamic-storage) instead.

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

The default normally uses the `data/` directory prepared during platform installation or local-cluster creation. Changing source folders, clones or worktrees does not select different storage.

Instances using the same storage reuse downloaded weights, tokenizers and model configuration. Runtime compilation caches, settings and profiling captures remain private to each instance, including instances in the same namespace.

| Directory | Contents |
|---|---|
| `data/shared/models/` | Managed model downloads shared by services |
| `data/deployments/` | Separate runtime state and profiling directories for each instance |
| `data/models/` | Model files supplied and maintained outside Foretoken's download cache |

Source-build caches are managed separately from model storage. See [Model sources](model-sources.md) to select ModelScope or a Hugging Face-compatible endpoint.

## Choose a directory

To use a different directory, save the following as `cache.yaml` in your deployment directory, using a path authorized by the platform administrator:

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: RuntimeCache
metadata:
  name: models
spec:
  directory: /srv/shared/foretoken-data
```

Add `cache.yaml` to the existing `resources` list in that directory's `kustomization.yaml`. If the instance already has a RuntimeCache and you are changing its directory or volume, delete the instance with `foretoken delete`. Then run `foretoken deploy` with the new storage configuration.

Relative paths resolve from the Kustomize directory. In a Git worktree, a relative path to the checkout's `data/` uses the primary checkout's `data/`. Explicit absolute paths keep their selected location.

For local k3d, the CLI prepares the repository data mount when creating the cluster. See the [k3d guide](../deployment/k3d.md). On a remote cluster, the selected directory must be available on the nodes that run its consumers; use a shared filesystem for multi-node access. A directory remains a directory selection rather than being replaced with an unrelated dynamically provisioned volume.

Move or copy user-maintained model files separately when changing their location.

## Share model files across namespaces

An administrator authorizes shared directories when installing the platform. From the repository root, installation can discover the default `data/` on an NFS mount visible to both the CLI host and cluster nodes. Local k3d uses its existing data bind mount.

Members deploy with the platform's default storage, or [select another authorized directory](#choose-a-directory) when needed. The platform prepares the namespace's storage binding when an instance first needs it; members do not need PVC names or cluster-wide storage permissions. Assign [namespace service permissions](../../cli/README.md#prepare-a-namespace-for-members) separately.

When the CLI host cannot inspect the shared mount, the administrator can bind a directory selection to an existing NFS-backed `ReadWriteMany` claim. Save the following in `deploy/member-storage.yaml`, replacing the directory and source claim with the intended storage:

```yaml
workload:
  cache:
    directories:
      models:
        directory: /srv/shared/foretoken-data
        sourceClaim: model-library/models
```

Add `--values deploy/member-storage.yaml` to the platform installation command. Source installations retain their `-e` and registry options.

Members select `/srv/shared/foretoken-data` as `spec.directory`. Native NFS and `nfs.csi.k8s.io` source claims are supported. A local filesystem on the CLI host alone does not provide shared remote storage; use a node-accessible directory or choose dynamic storage below.

## Stop, delete and reuse

`foretoken stop` releases serving resources while retaining the service's model-cache references. Resume with `foretoken deploy` to reuse those files.

`foretoken delete` releases the selected instance's model references after its workloads exit. Foretoken collects each managed model version when its last reference in that directory is gone. For example, if instances A and B use model X while C and D use model Y, deleting A and B can reclaim X without waiting for Y to stop.
The directory itself, user-supplied files in `data/models/`, and instance-local files remain. Borrowed platform-default volumes and administrator-provided storage remain available for later deployments. Dynamic volumes follow their configured retention and StorageClass reclaim policies.

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

To request a separate dynamically provisioned volume, save the following as `cache.yaml` in your deployment directory:

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: RuntimeCache
metadata:
  name: models
spec:
  initialSize: 10Gi
  accessMode: ReadWriteOnce
```

Add `cache.yaml` to the existing `resources` list in that directory's `kustomization.yaml`. If the instance already has a RuntimeCache and you are changing its directory or volume, delete the instance with `foretoken delete`. Then run `foretoken deploy` with the new storage configuration.

This requests a single-node writable volume from the default StorageClass. For multi-node access, use `ReadWriteMany` with a StorageClass that supports it. Add `storageClassName` to select another class, and `maxSize` only when its driver supports online expansion. Separate dynamically provisioned claims do not share files merely because they use the same StorageClass.

An administrator can set `workload.cache.claimName` to use an existing namespace-local PVC as the default. Explicit `directory` or `initialSize` selections take precedence over that default.

Profiling results stay in the instance's private data area; use [Profiling](../../benchmarks/docs/profile/README.md) to collect and view them.

## Distribute models across nodes

`foretoken install` automatically prepares or reuses shared model downloads. Models with persistent storage reuse cached files first. For anonymously accessible models on the official Hugging Face and ModelScope services, preparation uses node peers when the shared download service is available. Otherwise, files come directly from the model provider. Authenticated downloads and custom model endpoints use their provider directly.

Compatible replicas automatically reuse loaded weights when their runtime has the required CUDA and RDMA support. When weight sharing is unavailable, they load the prepared model files.

`foretoken uninstall` removes managed download services once no workloads or controllers use them; independently installed services are retained.
