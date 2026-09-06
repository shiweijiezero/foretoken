<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 持久化运行时缓存

[English](runtime-cache.md) | 简体中文

Foretoken 可以把已有 PVC 挂载为模型服务工作负载共享的运行时缓存。该能力默认关闭。启用后，model-server 通过配置的 runtime adapter 下载或加载模型制品，同时在同一份存储中保留推理引擎和编译产物，供后续 Pod 启动复用。

## 缓存保存什么

PVC 是存储边界，不是 Hugging Face 专用目录。每个 runtime adapter 负责自己的子目录和环境变量。以 vLLM 为例，缓存根目录可以使用类似布局：

```text
/var/cache/foretoken/
├── models/
├── vllm/
├── torch/
├── triton/
└── backend/
```

实际内容由 backend 负责。平台只提供稳定、可写的根目录，并保证工作负载重启后仍然使用相同的根目录。

## 配置已有 PVC

在平台 values 文件中配置：

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

`mountPath` 是每个工作负载容器内的绝对路径，不是宿主机路径。PVC、StorageClass、容量、访问模式、备份策略和清理策略由平台管理员负责。

`workload.cache` 只提供持久化存储。`runtime.vllm.modelSource` 是当前 vLLM adapter 拥有的可选输入，不会在 Foretoken 平台 API 中选择 provider；adapter 决定如何解释模型标识，以及是否需要这些输入。

如果 `claimName` 为空，持久化缓存关闭。Foretoken 不自动创建默认 PVC，因为存储类型、容量和多节点访问模式都依赖目标集群，无法安全地提供通用默认值。

## 存储要求

每个运行 `FrontendService` 或 `ModelService` 工作负载的命名空间都必须存在该 PVC。所有可能运行这些工作负载的节点都必须能够挂载该 PVC。

多副本或多节点部署需要支持并行挂载的共享存储，通常是 `ReadWriteMany`。常见实现包括 NFS、CephFS 和云厂商共享文件系统。只有在调度和副本拓扑能够保证兼容的单节点读写时，`ReadWriteOnce` 才适用。

## 运行时生命周期

```text
ModelService
  → ModelPool / ModelGroup
  → model-server 挂载运行时缓存
  → runtime adapter 下载或加载模型制品
  → model-server readiness 成功
  → ModelService 提交新的 serving generation
  → Frontend 使用已选中的 generation
```

模型加载由 model-server 进程负责。Controller 不运行 provider-specific snapshot downloader，也不直接把模型文件复制到 PVC。

请求新的模型 generation 时：

1. Controller 创建或更新目标 ModelGroup。
2. model-server 挂载配置的缓存，并执行 backend 正常的加载流程。
3. 在 ModelGroup 报告 ready 之前，新的 generation 不会被选中。
4. 新模型仍在下载、加载或编译时，旧的 serving generation 继续服务。
5. 新 generation ready 后，Controller 提交 serving generation，Frontend 再切换到它。

这样模型下载、引擎初始化和编译都处于同一个 readiness 边界内。Pod 重启时，如果缓存中已有对应产物，就不必重复完整的冷启动流程。

## 模型来源与 backend ownership

平台缓存不定义 `huggingface`、`modelscope` 或其他 provider enum。provider-specific 行为由启动 model-server 的 runtime adapter 负责。

因此 adapter 可以分别支持：

- Hugging Face 兼容仓库或 endpoint；
- ModelScope 仓库；
- 本地模型目录；
- 对象存储 URI；
- 推理引擎自己的权重缓存或编译缓存。

新增模型来源时，应扩展负责加载模型的 adapter 及其 loading contract，而不是继续向公共缓存 Controller 增加 provider 分支。

## 关闭或移除缓存

从平台 values 中移除 `workload.cache.claimName`，然后重新安装平台。新的工作负载 generation 将不再挂载 PVC，并恢复 runtime 原本的临时缓存行为。Foretoken 不会删除 PVC 或其中的文件。

需要清理缓存时，应在所有工作负载停止使用该 PVC 后，按照存储平台的正常 PVC 生命周期操作。

## 排查问题

- **Pod 无法挂载 PVC**：确认 PVC 存在于 workload namespace，并检查访问模式和 StorageClass 是否支持当前节点拓扑。
- **model-server 一直未 ready**：检查 ModelGroup 和 model-server 日志；下载、凭据、模型格式和编译错误由 runtime adapter 在日志中报告。
- **Frontend 一直等待**：Frontend 会跟随当前选中的 serving generation，只有 model-server ready 后才会切换到新的 generation。
- **Pod 重启后再次下载**：确认 Pod 使用相同的 claim 和根路径，并确认 runtime adapter 把缓存写入该根目录下。
- **编译仍然重复**：确认 adapter 的编译和 kernel cache 目录位于持久化根目录下，并确认 runtime image 与模型配置和已有缓存兼容。
