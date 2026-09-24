<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

Router 根据请求的模型、输入长度和能力要求，选择兼容且健康的目标；对于预填充/解码分离及编码/预填充/解码分离的服务，还会确保各阶段相互兼容。

例如，要优先选择等待请求较少的目标，可在 `FrontendService` 中配置：

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: queue_depth
```

只有需要调整路由策略时才填写 `spec.routerPipeline`。默认保留全部兼容目标（`allow_all`），用 `kv_least_loaded` 评分，再由 `gamble_sampling` 选取目标。各阶段通过 `algorithm` 选择算法；评分算法的可调选项写在 `scorer.parameters` 下。

| 阶段 | 算法 | 选择方式 |
| --- | --- | --- |
| Filter | `allow_all`（默认） | 保留所有兼容且健康的目标。 |
| Scorer | `kv_least_loaded`（默认） | 优先考虑可复用的 KV 前缀、已确认的缓存位置，再比较当前及下游 Decode 负载。 |
| Scorer | `least_loaded` · `uniform` | 优先选择低负载目标 · 为所有目标赋予相同分数。 |
| Scorer | `queue_depth` · `running_request` · `kv_cache_utilization` | 分别优先选择等待请求少、运行请求少或实测 KV 缓存占用低的目标。 |
| Scorer | `active_request` | 优先选择当前前端活跃请求较少的目标；可用 `idleThreshold`、`maxBusyScore` 调整。 |
| Scorer | `token_load` | 优先选择在途 token 和当前请求未缓存 prompt token 负载较低的目标；可用 `queueThresholdTokens` 调整。 |
| Scorer | `prefix` | 优先考虑可复用的 prompt 缓存块；可用 `matchLengthWeight`、`matchLengthScaleTokens` 调整匹配长度偏好。 |
| Picker | `gamble_sampling`（默认） | 根据完整分数排名采样：排名越高，选中概率越大；同分概率相同，低排名目标仍有机会被选中。 |
| Picker | `max` · `power_of_two_choices` | 选择最高分目标 · 随机抽取两个不同目标，选择分数较高者，同分时随机选取。 |

KV 索引不可用时，目标仍可参与路由，只是不享有 KV 前缀偏好。缓存位置的说明见 [KV 前缀索引](../kv-indexer/README_zh.md)。
