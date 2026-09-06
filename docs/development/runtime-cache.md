<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Persistent Runtime Cache

English | [简体中文](runtime-cache_zh.md)

Foretoken can mount an existing PVC as a shared runtime cache for model-serving workloads. The cache is optional. When it is enabled, model-server Pods download or load model artifacts through their configured runtime adapter, while the same storage retains engine and compilation artifacts for later Pod starts.

## What the cache stores

The PVC is a storage boundary, not a Hugging Face-specific directory. Each runtime adapter owns the subdirectories and environment variables it needs. A vLLM deployment may use a layout similar to:

```text
/var/cache/foretoken/
├── models/
├── vllm/
├── torch/
├── triton/
└── backend/
```

The exact contents are backend-owned. The platform only provides the writable root and keeps the root stable across workload restarts.

## Configure an existing PVC

Add the cache configuration to the platform values file:

```yaml
workload:
  cache:
    claimName: model-cache
    mountPath: /var/cache/foretoken

runtime:
  vllm:
    modelSource:
      endpoint: https://model-source.example.com
      tokenSecret:
        name: model-source-token
        key: token
```

`mountPath` is an absolute path inside each workload container. It is not a host path. The PVC, its `StorageClass`, capacity, access mode, backup policy, and cleanup policy remain platform-administrator responsibilities.

`workload.cache` only supplies persistent storage. The `runtime.vllm.modelSource` values are optional inputs owned by the current vLLM adapter. They do not select a provider in the Foretoken platform API; the adapter decides how to interpret the model identifier and whether these values are needed.

If `claimName` is empty, persistent caching is disabled. Foretoken does not create a default PVC because storage classes, capacity, and multi-node access modes are cluster-specific.

## Storage requirements

The claim must exist in every namespace where Foretoken creates `FrontendService` or `ModelService` workloads. Every node that may run those workloads must be able to mount the claim.

For multiple model Pods or multiple nodes, use shared storage that supports simultaneous mounts, normally `ReadWriteMany`. Typical implementations include NFS, CephFS, or a cloud-provider shared filesystem. A `ReadWriteOnce` claim is suitable only when the scheduling and replica topology guarantees one compatible writer and reader placement.

## Runtime lifecycle

```text
ModelService
  → ModelPool / ModelGroup
  → model-server mounts the runtime cache
  → runtime adapter downloads or loads model artifacts
  → model-server readiness succeeds
  → ModelService commits the new serving generation
  → Frontend consumes the selected generation
```

The model-server process owns model loading. The controller does not run a provider-specific snapshot downloader and does not copy model files into the PVC itself.

When a new model generation is requested:

1. The controller creates or updates the target ModelGroup.
2. The model-server mounts the configured cache and performs its normal backend loading path.
3. The new generation remains unselected until the ModelGroup reports readiness.
4. The previous serving generation remains selected while the new model is downloading, loading, or compiling.
5. After readiness, the controller commits the new serving generation and the Frontend switches to it.

This keeps model downloads, engine initialization, and compilation under the same readiness boundary. A restarted Pod can reuse artifacts already present in the cache instead of repeating the complete cold-start path.

## Model source and backend ownership

The platform cache does not define a `huggingface`, `modelscope`, or other provider enum. Provider-specific behavior belongs to the runtime adapter that starts the model server.

This boundary allows an adapter to support, for example:

- a Hugging Face-compatible repository or endpoint;
- a ModelScope repository;
- a local model directory;
- an object-storage URI;
- an engine-specific weight or compilation cache.

Adding a source requires extending the owning adapter and its loading contract. It should not add another provider-specific branch to the common cache controller.

## Disable or remove the cache

Remove `workload.cache.claimName` from the platform values and reinstall the platform. New workload generations stop mounting the PVC and return to the runtime's normal ephemeral cache behavior. Foretoken does not delete the PVC or its contents.

To remove cached data, use the storage platform's normal PVC lifecycle after all workloads have stopped using the claim.

## Troubleshooting

- **Pod cannot mount the PVC**: verify the PVC exists in the workload namespace and that its access mode and storage class support the node topology.
- **Model-server remains unready**: inspect the ModelGroup and model-server logs. The runtime adapter reports download, credential, model-format, and compilation failures there.
- **Frontend is waiting**: the Frontend follows the selected serving generation and will not switch to a generation whose model-server is not ready.
- **A restart downloads again**: verify that the Pod mounts the same claim and root path, and that the runtime adapter writes its cache below that root.
- **Compilation repeats**: verify that the adapter's compilation and kernel cache directories are under the persistent root and that the runtime image and model configuration are compatible with the existing cache.
