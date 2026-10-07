<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 请求路由

[English](README.md) | 简体中文

请求路由为请求选择模型、输入长度和能力要求都匹配的健康副本，也确保预填充/解码分离、编码/预填充/解码分离服务的各阶段相互兼容。

## 选择路由策略

优先选择等待请求较少的副本时，在 `FrontendService` 中设置评分算法：

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: queue_depth
```

重新部署前端配置后生效。不配置 `routerPipeline` 时，路由保留全部兼容且健康的目标（`allow_all`），通过 `kv_least_loaded` 评分，再由 `gamble_sampling` 选择。

根据工作负载选择评分算法：

| 目标 | 评分算法 |
| --- | --- |
| 兼顾输入缓存复用和负载均衡 | `kv_least_loaded`（默认） |
| 优先选择低负载目标 | `least_loaded` |
| 优先选择等待或运行请求较少的目标 | 分别使用 `queue_depth` 或 `running_request` |
| 优先选择实测 KV 缓存占用较低的目标 | `kv_cache_utilization` |
| 均衡当前前端副本上的活跃请求 | `active_request` |
| 优先选择模型服务等待队列较短的目标 | `load_aware` |
| 均衡当前前端副本上的未缓存输入 token 负载 | `token_load` |
| 优先复用输入前缀缓存 | `prefix` |
| 将冷请求分散到较久未选中的目标 | `no_hit_lru` |
| 在负载均衡与缓存复用之间折中 | `two_tier`，必须搭配 `picker.algorithm: max` |
| 所有目标得分相同 | `uniform` |

评分算法的选项放在 `scorer.parameters` 下。例如，提高匹配前缀长度的权重：

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: prefix
      parameters:
        matchLengthWeight: 0.5
```

KV 索引不可用时，目标仍可参与路由，只是不享有缓存偏好。支持的缓存及状态访问见 [KV 前缀索引](../kv-indexer/README_zh.md)。

## 根据得分选择目标

通过 `routerPipeline.picker.algorithm` 选择：

| 选择算法 | 行为 |
| --- | --- |
| `gamble_sampling`（默认） | 排名越高，选中概率越大；同分概率相同，低排名目标仍有机会被选中。 |
| `max` | 选择最高分目标。 |
| `power_of_two_choices` | 随机抽取两个不同目标，选择分数较高者，同分时随机选择。 |

## 查看路由表现

向服务发送流量时，在 [Grafana](../../../../observability/README_zh.md) 的路由选择区域比较选择份额、结果、可选目标和选择耗时。

请求限额与排队单独通过前端的[准入规则](../../README_zh.md#配置准入规则)配置。
