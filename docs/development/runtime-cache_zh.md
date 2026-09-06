<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 持久化运行时缓存

[English](runtime-cache.md) | 简体中文

使用已有 PVC 保存模型和编译缓存，使 Pod 重启后可以复用之前的工作。`claimName` 为空时，该功能关闭。

```yaml
workload:
  cache:
    claimName: model-cache
    mountPath: /var/cache/foretoken
```

每个 workload namespace 都必须提前创建 PVC，并确保所有可能运行工作负载的节点都能挂载它。多节点部署通常需要 `ReadWriteMany`。`mountPath` 是容器内路径，不是宿主机路径。Foretoken 不创建或删除 PVC。

model-server 负责下载或加载模型，并把可复用的运行时产物写入缓存。新的 ModelGroup ready 后才会选中新 serving generation；新 generation 启动期间，旧 generation 继续被选中。

移除 `workload.cache.claimName` 可关闭持久化缓存。只有在所有使用该 PVC 的工作负载停止后，才应删除 PVC。
