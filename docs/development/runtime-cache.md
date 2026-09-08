<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Persistent Runtime Cache

English | [简体中文](runtime-cache_zh.md)

Add `cache.yaml` to the same Kustomize deployment as the model resources:

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: RuntimeCache
metadata:
  name: models
spec:
  initialSize: 10Gi
  maxSize: 100Gi
```

`foretoken deploy` creates the PVC through the namespace's default `StorageClass`. `initialSize` is only the first PVC request. Setting `maxSize` enables automatic growth: Foretoken requests more capacity when the lowest observed free-space ratio reaches 20%, doubling the request up to `maxSize`. This is an early-growth policy, not a guarantee that an active download cannot fill the filesystem before expansion completes. You can increase `maxSize` later, but cannot reduce or remove it. PVC shrinking is not supported.

If the persistent cache becomes unwritable while a new model-server is starting, Foretoken stops that EngineCore process and retries once with cache directories under the Pod's temporary `/tmp` volume. A frontend whose persistent snapshot is missing downloads its tokenizer files into its own temporary volume. Foretoken does not clear the PVC, move already-running instances, or switch EngineCore paths without restarting the failed child process. Temporary caches are lost with their Pods.

The defaults are `ReadWriteMany` and `Retain`. The `StorageClass` must support the selected access mode. Automatic growth requires online volume and filesystem expansion; Foretoken does not restart Pods to complete offline filesystem resize. Set `retentionPolicy: Delete` to remove the PVC after workloads stop using it.

An administrator can instead configure `workload.cache.claimName` during platform installation. Foretoken does not modify or delete an existing claim.
