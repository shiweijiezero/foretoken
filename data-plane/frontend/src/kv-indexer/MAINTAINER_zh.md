<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV 索引维护指南

[English](MAINTAINER.md) | 中文

KV 索引消费 backend adapter 发布的事件，并为 Router 评分提供缓存位置观测。它不负责保存或搬运缓存。

## 协议边界

每个事件源按模型 revision、KV scope、route target 和 data-parallel rank 隔离。事件流使用 epoch 和连续 cursor。事件缺失、乱序或不兼容时，事件源必须退化为 `Unavailable`，不能暴露错误的缓存位置匹配。

通用协议可以表达 `Device`、`HostPinned`、`Disk` 和 `External` placement。Foretoken 当前的 serving projection 只启用本地 `Device` placement，并禁用缓存恢复和传输。在控制器、runtime、transport、诊断和 rollout 路径完整支持前，不能把通用 enum 写成产品能力。

## 索引解析

`PositionalHashIndex` 和 `RadixTreeIndex` 是由 topology-aware 配置选择的内部实现。`NoopKvPrefixIndexer` 是位置不可用时 Router 使用的 no-op 实现。它们都不是用户可配置的 `FrontendService` 选项。

## Backend adapter

后端专有的 block 标识、事件生命周期和 placement 语义属于 backend adapter。扩展 adapter 时必须保持精确的事件源身份和序号规则。后端需要提供足够完整的事件，使索引能够区分已确认匹配、已确认未命中和不可用观测。

修改协议或索引行为时，应同步更新面向用户的 KV 指南、Router 指南、runtime 诊断、指标和 contract tests。
