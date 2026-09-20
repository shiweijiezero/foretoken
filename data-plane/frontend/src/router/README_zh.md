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
    picker: round_robin
```

| 阶段 | 当前可选值 | 默认值 | 作用 |
| --- | --- | --- | --- |
| Filter | `allow_all` | `allow_all` | 保留全部兼容且健康的目标 |
| Scorer | `kv_least_loaded`、`least_loaded`、`uniform`、`queue_depth`、`running_request`、`kv_cache_utilization` | `kv_least_loaded` | 为保留目标评分 |
| Picker | `max`、`round_robin` | `round_robin` | 从最高分目标中选择一个 |

每个 pipeline 阶段都通过名称选择算法。如果部署提供了其他路由实现，也可以在相同的 `routerPipeline` 字段中填写对应名称。

`kv_least_loaded` 优先选择可复用前缀更长的目标；长度相同时，依次比较已确认的设备、本机 CPU、本机磁盘和外部 Store，再比较负载。无法提供完整缓存身份的层级不会获得位置偏好。`least_loaded` 忽略 KV 位置，只按当前请求负载评分。`uniform` 为所有候选项赋予相同分数；`round_robin` 会在同分目标之间按确定顺序轮转，`max` 则选择一个确定的同分目标。

将 `scorer` 设为 `queue_depth`，可优先选择调度器中等待请求较少的目标；设为 `running_request`，可优先选择运行请求较少的目标；设为 `kv_cache_utilization`，可优先选择实测 KV cache 使用率较低的目标。

路由会区分同一模型执行组内的各个 DP rank。负载策略使用对应 rank 的当前调度器计数，使用率策略使用对应 rank 的 KV Cache 使用率；收到首个遥测响应即可评分，无需等满速率窗口。缺失观测不代表零负载：有实测值的候选优于未知候选，全部未知时仍由 Picker 选择。这三个纯指标策略不叠加前缀位置、待派发请求或下游阶段负载。

Frontend 的 `/metrics` 按 `model_name` 输出路由结果和耗时。成功选择目标时，`foretoken_router_target_selections_total` 按 `model_name`、`model_role`、`route_target_id` 和 `data_parallel_rank` 计数，表示路由选择，不因后续准入、生成失败或取消而改写。模型与目标标签只来自服务配置。

只有模型、输入限制、请求能力和目标健康状态都兼容时，请求才会成为候选项。对于预填充/解码分离或编码/预填充/解码分离的服务，路由会确保选中的各阶段彼此兼容。

KV 索引返回 `Unavailable` 时，目标仍可参与路由，但不获得 KV 前缀匹配优先权；路由仍会考虑其负载。位置查询和退化行为见 [KV 前缀索引](../kv-indexer/README_zh.md)。

编译进二进制的路由算法，以及 Filter、Scorer 和 Picker 的精确维护契约见 [Router 维护指南](MAINTAINER_zh.md)。
