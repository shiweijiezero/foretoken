<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Persistent Runtime Cache

English | [简体中文](runtime-cache_zh.md)

Foretoken can mount an existing PVC as a shared runtime cache. Model-server Pods use it for model files and runtime compilation artifacts, so a restart can reuse work from an earlier start. The feature is optional and disabled when `claimName` is empty.

## Configuration

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

`mountPath` is an absolute path inside the container, not a host path. The PVC must already exist in every workload namespace. The storage must be mountable from every node that can run the workloads; multi-node deployments normally need `ReadWriteMany` storage.

`runtime.vllm.modelSource` is optional and is interpreted by the vLLM adapter. It is separate from the cache location. If no source access is required, omit it.

Foretoken does not create, resize, back up, or delete the PVC. If `claimName` is empty, workloads use their normal ephemeral runtime storage.

## Serving behavior

The model-server owns model loading. When a new ModelGroup starts, it mounts the cache and performs its normal backend loading and compilation path. The controller selects the new serving generation only after the ModelGroup is ready; the previous generation remains selected while the new one is starting.

Frontend workloads use the cache after the selected model generation is ready. A restarted workload can reuse model and compilation artifacts already stored under the cache root.

## Disable and cleanup

Remove `workload.cache.claimName` and reinstall the platform to stop mounting the PVC for new workload generations. Foretoken leaves the PVC and its contents unchanged. Remove the PVC only after all workloads using it have been stopped.

If a Pod cannot mount the cache, check the PVC namespace, access mode, StorageClass, and node availability. If a model-server remains unready or recompiles, inspect its logs and verify that the runtime image and model configuration match the existing cache.
