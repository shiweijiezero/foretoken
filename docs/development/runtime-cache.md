<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Persistent Runtime Cache

English | [简体中文](runtime-cache_zh.md)

Create one `RuntimeCache` in a workload namespace to preserve model and compilation caches across Pod restarts:

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: RuntimeCache
metadata:
  name: models
  namespace: foretoken-demo
spec:
  size: 100Gi
```

Foretoken creates the PVC with `ReadWriteMany` and `Retain` by default. The namespace's default `StorageClass` must support the selected access mode. Model and Frontend workloads automatically use the single Ready `RuntimeCache` in their namespace.

Increase `spec.size` to expand the PVC. PVC shrinking is not supported.

Deleting the `RuntimeCache` retains the PVC by default. Set `spec.retentionPolicy: Delete` to delete it after workloads stop using it.

An administrator can instead configure `workload.cache.claimName` during platform installation to use an existing PVC. Foretoken never modifies or deletes an existing claim.
