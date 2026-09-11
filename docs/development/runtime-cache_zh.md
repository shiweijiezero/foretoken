<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 运行时缓存生命周期

[English](runtime-cache.md) | 简体中文

部署配置见[模型存储](../model-storage_zh.md)。

## 存储职责

命名空间中的 RuntimeCache 管理 PVC，并发布 claim 名称与就绪状态。工作负载控制器将该 claim 挂载给 model-server 和 frontend。管理员提供的 `workload.cache.claimName` 优先使用，不由 RuntimeCache 控制器管理。

动态缓存通过 StorageClass 申请存储。配置 `maxSize` 后，控制器在各挂载点观测到的最低空闲比例达到 20% 时申请扩容，将容量逐次翻倍至上限。扩容申请失败时，工作负载仍可以使用已经绑定的卷。

目录模式中，CLI 在提交用户配置前解析节点可见路径。RuntimeCache 控制器创建 PVC，决定 claim 名称、volume 名称、访问模式和绑定容量；CLI 读取该 claim 来准备静态 PV，不维护另一套命名和容量计算。绑定容量不会创建文件系统配额。

本地 k3d 挂载和单节点目录会配置 PV 节点亲和性。多节点绝对路径表示同一个文件系统已在所有节点的对应位置共享，不负责安装共享存储或跨节点传输文件。

## 保留与重部署

保留已删除缓存时，控制器移除 PVC 的 owner。新的目录缓存只有在路径、卷绑定和归属一致时，才能重新接管原 claim。删除 Namespace 仍会删除其中的 PVC。

CLI 默认保留目录 PV。Namespace 重建后，旧 claim 已不存在时，可以把同一 PV 重新绑定到控制器创建的新 PVC；资源版本和旧 claim 前置条件防止覆盖并发绑定修改。其他部署拥有的 PV，或路径、节点位置不同的 PV，不会被接管。

设置 `retentionPolicy: Delete` 时，控制器在工作负载释放 PVC 后删除它，CLI 随后删除自己的目录 PV 对象。PV 回收策略仍为 Retain，不删除文件。动态卷使用其 StorageClass 的回收策略。

## 模型文件与运行时缓存

model-files 库统一负责数据面两端的本地模型和 tokenizer 目录解析。相对引用限制在挂载根目录内，单个文件会明确报错，不会被替换成父目录。库不下载模型，也不校验模型格式；这些职责属于推理引擎与 tokenizer loader。本地文件解析后，公开模型标识保持不变。

运行时缓存挂载保存模型来源服务的缓存以及引擎编译缓存。model-server 拥有启动写入检查和一次临时缓存重试：先停止失败的 EngineCore，再在 Pod 的 `/tmp` 下重试。Frontend 缺少 Hub 缓存时可使用临时 tokenizer 缓存。该回退不会切换已运行引擎的存储路径，也不会复制预先准备的本地 checkpoint。
