<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

Router 为每个推理请求选择兼容且健康的模型目标。

在 `FrontendService.spec.routerPipeline` 中配置路由策略：

```yaml
spec:
  routerPipeline:
    filter: allow_all
    scorer: kv_least_loaded
    picker: weighted_random
```

| 阶段 | 当前可选值 | 默认值 | 作用 |
| --- | --- | --- | --- |
| Filter | `allow_all` | `allow_all` | 保留全部兼容且健康的目标 |
| Scorer | `kv_least_loaded`、`least_loaded`、`uniform`、`queue_depth`、`running_request`、`kv_cache_utilization` | `kv_least_loaded` | 按所选策略为目标评分 |
| Picker | `weighted_random`、`max`、`power_of_two_choices` | `weighted_random` | 按路由分数从高到低的排名采样目标 |

`kv_least_loaded` 优先比较可复用的 KV 前缀长度，再比较已确认的缓存层级和本地性，最后比较当前及下游 Decode 负载。无法提供完整缓存身份的层级不会获得位置偏好。`HostPinned` 是用于 KV 卸载的页锁定主机内存。`least_loaded` 只比较负载，`uniform` 为所有候选赋予相同分数。

`weighted_random` 根据完整分数的排名生成采样权重，同分候选权重相等；`max` 选择最高分候选；`power_of_two_choices` 随机抽取两个不同候选，选择分数更高者，同分时随机选择。只有两个候选时，P2C 会比较两者，不能阻止请求集中到分数更高的一方。

将 `scorer` 设为 `queue_depth`，可优先选择调度器中等待请求较少的目标；设为 `running_request`，可优先选择运行请求较少的目标；设为 `kv_cache_utilization`，可优先选择实测 KV Cache 使用率较低的目标。Picker 按所选的采样或最高分规则进行选择。

路由会区分同一模型执行组内的各个 DP rank。负载策略使用对应 rank 的当前调度器计数，使用率策略使用对应 rank 的 KV Cache 使用率；收到首个遥测响应即可评分，无需等满速率窗口。缺失观测不代表零负载：有实测值的候选优于未知候选，全部未知时仍由 Picker 选择。这三个纯指标策略不叠加前缀位置、待派发请求或下游阶段负载。

只有模型、输入限制、请求能力和目标健康状态都兼容时，请求才会成为候选项。对于预填充/解码分离或编码/预填充/解码分离的服务，路由会确保选中的各阶段彼此兼容。

KV 索引返回 `Unavailable` 时，目标仍可参与路由，但不获得 KV 前缀匹配优先权；路由仍会考虑其负载。位置查询和退化行为见 [KV 前缀索引](../kv-indexer/README_zh.md)。

编译进二进制的路由算法，以及 Filter、Scorer 和 Picker 的精确维护契约见 [Router 维护指南](MAINTAINER_zh.md)。
