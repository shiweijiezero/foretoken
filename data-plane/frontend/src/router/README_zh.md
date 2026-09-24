<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

Router 为每个推理请求选择兼容且健康的模型目标。

在 `FrontendService.spec.routerPipeline` 中配置路由策略：

```yaml
spec:
  routerPipeline:
    filter:
      algorithm: allow_all
    scorer:
      algorithm: kv_least_loaded
    picker:
      algorithm: weighted_random
```

| 阶段 | 当前可选值 | 默认值 | 作用 |
| --- | --- | --- | --- |
| Filter | `allow_all` | `allow_all` | 保留全部兼容且健康的目标 |
| Scorer | `kv_least_loaded`、`least_loaded`、`uniform`、`queue_depth`、`running_request`、`kv_cache_utilization`、`active_request`、`token_load`、`prefix` | `kv_least_loaded` | 为保留目标评分 |
| Picker | `weighted_random`、`max`、`power_of_two_choices` | `weighted_random` | 按路由分数从高到低的排名采样目标 |

`kv_least_loaded` 优先比较可复用的 KV 前缀长度，再比较已确认的缓存层级和本地性，最后比较当前及下游 Decode 负载。无法提供完整缓存身份的层级不会获得位置偏好。`HostPinned` 是用于 KV 卸载的页锁定主机内存。`least_loaded` 只比较负载，`uniform` 为所有候选赋予相同分数。

`weighted_random` 根据完整分数的排名生成采样权重，同分候选权重相等；`max` 选择最高分候选；`power_of_two_choices` 随机抽取两个不同候选，选择分数更高者，同分时随机选择。只有两个候选时，P2C 会比较两者，不能阻止请求集中到分数更高的一方。

将 `scorer.algorithm` 设为 `queue_depth`，可优先选择调度器中等待请求较少的目标；设为 `running_request`，可优先选择运行请求较少的目标；设为 `kv_cache_utilization`，可优先选择实测 KV Cache 使用率较低的目标。Picker 按所选的采样或最高分规则进行选择。

将 `scorer.algorithm` 设为 `active_request`，即可优先选择当前 frontend 跟踪的活跃请求较少的目标；如需调整默认行为，可配置 `scorer.parameters.idleThreshold` 和 `scorer.parameters.maxBusyScore`。

将 `scorer.algorithm` 设为 `token_load`，即可优先选择 frontend 本地在途 token 负载较低、且当前请求未缓存 prompt token 较少的目标；可通过 `scorer.parameters.queueThresholdTokens` 调整饱和阈值。

将 `scorer.algorithm` 设为 `prefix`，即可优先选择可复用 prompt 前缀更长的目标；可配置 `scorer.parameters.matchLengthWeight` 和 `scorer.parameters.matchLengthScaleTokens` 增加归一化的匹配长度偏好。

只有健康、支持所请求模型、输入限制和请求能力的目标才会参与路由。对于预填充/解码分离或编码/预填充/解码分离的服务，路由会保持选中阶段之间的兼容关系。

KV 索引不可用时，目标仍可参与路由，但不获得 KV 前缀匹配优先权。缓存位置行为见 [KV 前缀索引](../kv-indexer/README_zh.md)。
