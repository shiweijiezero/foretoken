<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 运行时缓存生命周期

[English](runtime-cache.md) | 简体中文

部署配置见[模型存储](../features/model-storage_zh.md)。

## 存储职责

命名空间中的 RuntimeCache 管理 PVC，并发布 claim 名称与就绪状态。工作负载控制器将该 claim 挂载给 model-server 和 frontend。`workload.cache.claimName` 指定的 PVC 优先使用，由管理员管理。

动态缓存通过 StorageClass 申请存储。配置 `maxSize` 后，控制器在各挂载点观测到的最低空闲比例达到 20% 时申请扩容，将容量逐次翻倍至上限。扩容申请失败时，工作负载仍可以使用已经绑定的卷。

目录模式中，CLI 在提交用户配置前解析节点可见路径。RuntimeCache 控制器创建 PVC，决定 claim 名称、volume 名称、访问模式和绑定容量；CLI 读取该 claim 来准备静态 PV。

远端目录使用 ReadWriteOnce 时，共享缓存的模型及各 Pool 的节点选择范围必须限定在同一个节点。ReadWriteMany 要求集群各节点的目录指向同一共享文件系统。本地 k3d 根据节点的实际绑定挂载设置 PV 节点亲和性。

## 保留与重部署

保留已删除缓存时，控制器移除 PVC 的 owner。新的目录缓存只有在路径、卷绑定和归属一致时，才能重新接管原 claim。删除 Namespace 仍会删除其中的 PVC。

CLI 默认保留目录 PV。Namespace 重建后，旧 claim 已不存在时，可以把同一 PV 重新绑定到控制器创建的新 PVC；资源版本和旧 claim 前置条件防止覆盖并发绑定修改。

设置 `retentionPolicy: Delete` 时，控制器在工作负载释放 PVC 后删除它，CLI 随后删除自己的目录 PV 对象。PV 回收策略为 Retain，保留目录文件。动态卷使用其 StorageClass 的回收策略。

## 模型文件与运行时缓存

平台统一在数据挂载下派生模型根目录，分别投射模型目录、来源缓存和编译缓存的位置。artifacts 库统一负责数据面两端的本地模型和 tokenizer 目录解析。相对引用解析为模型根目录下的目录。推理引擎与 tokenizer loader 负责下载和格式校验。本地文件解析后，公开模型标识保持不变。

运行时缓存挂载保存模型来源缓存以及 vLLM、TorchInductor 等引擎编译缓存。单节点服务的持久缓存写入检查失败时，model-server 会先停止失败的 EngineCore，再在 Pod 的 `/tmp` 下重试一次。多节点成员统一重启。Frontend 缺少 Hub 缓存时可使用临时 tokenizer 缓存。

Triton 的初始缓存使用 PVC 上的 `triton/<节点名>`，隔离不同节点的写入，同节点 Pod 重建后可复用。缓存位置通过 Group 滚动更新切换，旧 Group 在被替换前保持原环境。引擎自身的编译器子目录仍由 vLLM 管理。

持久数据根目录包含 `models`、`vllm`、`torch`、`triton` 和 `profiles`。自备模型从 `models` 解析；Hub 仓库保留上游的 `models/hub` 布局和 revision 缓存查找。诊断采集发布到 `profiles/runs`，临时缓存重试期间不可用。
