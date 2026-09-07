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
  maxSize: 100Gi
```

`foretoken deploy` 会通过命名空间的默认 `StorageClass` 创建 PVC。`initialSize` 只决定首次 PVC 请求。设置 `maxSize` 后，Foretoken 会在各挂载点上报的最低空闲比例降至 20% 时申请扩容，并将请求容量逐次翻倍至 `maxSize`。这是提前扩容策略，不能保证扩容完成前的下载一定不会写满文件系统。后续可以增大 `maxSize`，但不能减小或移除。PVC 不支持缩容。

新 model-server 启动期间如果持久化缓存无法继续写入，Foretoken 会停止该 EngineCore 子进程，并在 Pod 的临时 `/tmp` 卷中使用缓存目录重试一次。Frontend 找不到持久化 snapshot 时，会把 tokenizer 文件下载到自己的临时卷。该过程不会清空 PVC、迁移已经正常运行的实例，也不会在不中止失败子进程的情况下切换 EngineCore 写入路径。临时缓存会随 Pod 删除。

默认访问模式为 `ReadWriteMany`，保留策略为 `Retain`。`StorageClass` 必须支持所选访问模式。自动扩容要求 CSI 支持在线卷扩容和文件系统扩容；Foretoken 不会通过重启 Pod 完成离线文件系统扩容。设置 `retentionPolicy: Delete` 后，工作负载停止使用 PVC 时会将其删除。

平台管理员也可以在安装时通过 `workload.cache.claimName` 使用已有 PVC。Foretoken 不修改或删除已有 PVC。
