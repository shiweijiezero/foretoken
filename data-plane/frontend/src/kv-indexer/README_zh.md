<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV 前缀索引

多个请求使用同一段较长的系统提示词时，某个模型实例可能还保留着处理这段前缀时生成的 KV 缓存。KV 前缀索引告诉 Router 哪个实例曾报告过这些缓存，帮助请求复用已有计算。缓存仍由推理后端保存，路由目前考虑的是目标实例本地加速器上的缓存。

使用 [`kv_least_loaded` 路由评分策略](../router/README_zh.md)时，查询结果按以下方式影响选择：

- **匹配（match）**：优先选择缓存前缀更长的目标，再比较负载。
- **未命中（miss）**：索引中未找到该目标可复用的前缀，不给予缓存优先权。
- **`Unavailable`**：索引无法可靠判断，不等于未命中；该目标不获得缓存优先权，但仍参与常规路由。

目标仍需健康且满足请求要求。匹配能提高复用的可能性，但后端可能在请求开始执行前淘汰缓存，因此不保证实际命中。

## 运维

通过前端的 `/statusz` 查看 KV 索引健康状态和退化原因，通过 `/metrics` 接入 Prometheus 监控。若索引持续退化，根据报告的原因检查模型服务的缓存更新。访问方式见[前端接口访问范围](../../README_zh.md#接口访问范围)。

协议细节和后端接入说明见 [KV 索引维护指南](MAINTAINER_zh.md)。
