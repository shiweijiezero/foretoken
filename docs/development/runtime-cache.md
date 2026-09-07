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
  expansion:
    mode: Automatic
    reserve: 10Gi
    maxSize: 100Gi
```

`foretoken deploy` creates the PVC through the namespace's default `StorageClass`. Automatic expansion maintains the configured free-space reserve up to `maxSize`; model loading waits until that reserve is available. PVC shrinking is not supported. Omit `expansion` to keep the initial size fixed.

The defaults are `ReadWriteMany` and `Retain`. The `StorageClass` must support the selected access mode and volume expansion. Set `retentionPolicy: Delete` to remove the PVC after workloads stop using it.

An administrator can instead configure `workload.cache.claimName` during platform installation. Foretoken does not modify or delete an existing claim.
