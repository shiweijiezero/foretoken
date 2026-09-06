<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Persistent Runtime Cache

English | [简体中文](runtime-cache_zh.md)

Use an existing PVC to preserve model and compilation caches across Pod restarts. The feature is disabled when `claimName` is empty.

```yaml
workload:
  cache:
    claimName: model-cache
    mountPath: /var/cache/foretoken
```

The PVC must already exist in every workload namespace and be mountable from every eligible node. Multi-node deployments normally require `ReadWriteMany`. `mountPath` is a container path, not a host path. Foretoken does not create or delete the PVC.

The model server downloads or loads the model and writes reusable runtime artifacts to the cache. A new serving generation is selected only after its ModelGroup is ready; the previous generation remains selected while the new one starts.

Remove `workload.cache.claimName` to disable the persistent cache. Delete the PVC only after all workloads using it have stopped.
