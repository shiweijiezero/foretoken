<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 持久化运行时缓存

[English](runtime-cache.md) | 简体中文

Foretoken 可以把已有 PVC 挂载为共享运行时缓存。model-server 使用它保存模型文件和运行时编译产物，Pod 重启后可以复用之前完成的工作。该功能默认关闭；`claimName` 为空时不会启用。

## 配置

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

`mountPath` 是容器内的绝对路径，不是宿主机路径。每个 workload namespace 都必须提前创建同名 PVC。所有可能运行这些工作负载的节点都必须能够挂载该存储；多节点部署通常需要 `ReadWriteMany`。

`runtime.vllm.modelSource` 是可选配置，由 vLLM adapter 解释，与缓存位置相互独立。如果不需要额外的模型来源访问配置，可以省略它。

Foretoken 不创建、扩容、备份或删除 PVC。`claimName` 为空时，工作负载继续使用原有的临时运行时存储。

## 服务行为

模型加载由 model-server 负责。新的 ModelGroup 启动后，会挂载缓存并执行 backend 正常的模型加载和编译流程。只有 ModelGroup ready 后，Controller 才会选中新 serving generation；新 generation 启动期间，旧 generation 继续被选中。

Frontend 会在选中的模型 generation ready 后使用该缓存。工作负载重启时，可以复用缓存根目录中已经保存的模型和编译产物。

## 关闭与清理

移除 `workload.cache.claimName` 并重新安装平台，新的 workload generation 将不再挂载该 PVC。Foretoken 不会修改 PVC 或其中的文件。只有在所有使用该 PVC 的工作负载停止后，才应删除 PVC。

如果 Pod 无法挂载缓存，请检查 PVC 所在命名空间、访问模式、StorageClass 和节点可用性。如果 model-server 一直未 ready 或重复编译，请查看其日志，并确认 runtime image、模型配置与已有缓存匹配。
