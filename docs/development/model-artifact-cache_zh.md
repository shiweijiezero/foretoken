<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型制品缓存

[English](model-artifact-cache.md) | 简体中文

可选的模型缓存把 Hugging Face 快照准备与推理服务分开。平台管理员提供已有的命名空间内 PVC，并确保所有工作负载节点都能挂载；多节点部署通常需要 `ReadWriteMany` 存储。ModelService 负责准备 Job 和状态；平台负责缓存保留；Frontend 与 model-server 只消费准备完成的文件。

## Ownership 与生命周期

```text
ModelService
  → 准备 Job
  → ArtifactsReady
  → ModelPools
  → ModelGroups
```

Job 完成前，ModelService Controller 不修改目标 ModelPool。已有 serving generation 会在新制品准备期间继续被选择。Job 失败时发布 `ArtifactsReady=False`，reason 为 `PreparationFailed`。

Job 使用配置的 model-server 镜像和 `foretoken-prepare-hf-snapshot` 命令，接收可选 endpoint 和 token Secret，写入标准 Hugging Face cache 布局，然后退出。长期运行的 Frontend 和 model-server Pod 不接收 endpoint 或 token；它们挂载缓存，并使用 `HF_HUB_OFFLINE=1`。

PVC 不由 ModelService 拥有或删除，因此快照可以跨服务删除保留，并由其他服务复用。容量、访问模式、备份和缓存清理由平台负责。

## Runtime 边界

`ModelService.spec.model` 继续作为公开模型身份和 Hugging Face repository ID。首个实现沿用标准 Hugging Face cache 布局，不向用户暴露内部制品路径。Frontend 依次解析本地目录和本地缓存；只有未启用托管缓存时才访问 Hub。

当前生命周期不加入自动 PVC 容量、默认镜像站、存储 provider registry、内容 hash 或缓存淘汰。只有出现明确的独立使用方和 ownership 后，才引入这些能力。
