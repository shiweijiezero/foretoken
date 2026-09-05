<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV 前缀索引

KV 前缀索引向 Router 提供 prompt 前缀的缓存位置。它是缓存目录，不是缓存本身：KV block 仍由推理后端保存，索引不会保存、恢复或传输缓存。

## 当前行为

当前控制器投影只发布本地 `Device` KV 位置。Foretoken 当前不把 CPU 内存或磁盘 offload、远端缓存共享或点对点缓存传输作为路由能力。

对于支持查询的请求，索引可能返回已确认的前缀匹配、已确认的未命中，或 `Unavailable`。`Unavailable` 表示索引当前无法可靠回答，不等于未命中。Router 会把它视为没有 KV 位置偏好，继续常规路由。

缓存位置只是提示。它表示精确模型 revision、KV scope 和运行时分区内的完整 token block 前缀，不保证推理真正开始时后端仍保有缓存。使用 cache salt、LoRA、不支持的多模态特性或显式跳过 prefix cache 的请求不会使用 KV 前缀查询。

## 运维

Frontend 通过 model-server 事件刷新 KV 位置。事件源不可达、缺少密钥，或 cursor、epoch 不一致时，普通服务仍可继续，但该事件源的 KV 感知路由会变为不可用或退化。

平台运维者可以通过 Frontend 的集群内 `/statusz` 查看 KV 索引状态和事件源健康度，通过 `/metrics` 由 Prometheus 抓取指标。应用客户端不需要修复 KV 同步；应由平台运维者检查 model-server 事件源和 serving 配置。

Router 会将该信号与负载和请求兼容性结合。路由行为见 [Router 指南](../router/README_zh.md)。

通用 placement 词汇、事件序号、索引实现和后端适配要求见 [KV 索引维护指南](MAINTAINER_zh.md)。
