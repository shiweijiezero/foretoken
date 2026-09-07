<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 持久化运行时缓存

[English](runtime-cache.md) | 简体中文

在模型部署目录中加入 `cache.yaml`，并将其列入同一个 Kustomize 配置：

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

`foretoken deploy` 会通过命名空间的默认 `StorageClass` 创建 PVC。自动扩容会在 `maxSize` 范围内维持指定的可用空间，模型加载会等待该空间就绪。PVC 不支持缩容；不配置 `expansion` 时，容量固定为 `initialSize`。

默认访问模式为 `ReadWriteMany`，保留策略为 `Retain`。`StorageClass` 必须支持所选访问模式和卷扩容。设置 `retentionPolicy: Delete` 后，工作负载停止使用 PVC 时会将其删除。

平台管理员也可以在安装时通过 `workload.cache.claimName` 使用已有 PVC。Foretoken 不修改或删除已有 PVC。
