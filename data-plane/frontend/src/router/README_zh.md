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
| Scorer | `kv_least_loaded`、`least_loaded`、`uniform`、`queue_depth` | `kv_least_loaded` | 为保留目标评分 |
| Picker | `max`、`round_robin` | `round_robin` | 从最高分目标中选择一个 |

每个 pipeline 阶段都通过名称选择算法。如果部署提供了其他路由实现，也可以在相同的 `routerPipeline` 字段中填写对应名称。

`kv_least_loaded` 优先考虑已确认的本地 KV 前缀位置，再选择负载较低的目标。`least_loaded` 忽略 KV 位置，只按当前请求负载评分。`uniform` 为所有候选项赋予相同分数；`round_robin` 会在同分目标之间按确定顺序轮转，`max` 则选择一个确定的同分目标。

将 `scorer` 设为 `queue_depth`，即可优先选择引擎调度器中等待的请求较少的目标。

该策略只使用 Model Server 端点的当前指标，不叠加前缀位置、待派发请求或下游阶段负载。
同一 Model Server 的所有 DP rank 共享端点评分，无法通过该策略区分各 rank 的负载。
收到首个遥测响应后即可使用 gauge，无需等满速率统计窗口。未观测到的指标按零处理；
后续响应缺失某项指标时，仅在遥测历史有效的情况下保留该项之前的值。
时间戳或计数器发生重置后，缺失指标保持未观测状态，直到生产者重新上报。

只有健康且支持请求指定模型、输入长度和所需能力的目标才会成为候选项。对于预填充/解码分离或编码/预填充/解码分离的服务，路由会确保选中的各阶段彼此兼容。

KV 索引返回 `Unavailable` 时，目标仍可参与路由，但不获得 KV 前缀匹配优先权；路由仍会考虑其负载。位置查询和退化行为见 [KV 前缀索引](../kv-indexer/README_zh.md)。

编译进二进制的路由算法，以及 Filter、Scorer 和 Picker 的精确维护契约见 [Router 维护指南](MAINTAINER_zh.md)。
