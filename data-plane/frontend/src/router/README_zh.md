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
| Scorer | `kv_least_loaded`、`least_loaded`、`uniform` | `kv_least_loaded` | 为保留目标评分 |
| Picker | `max`、`round_robin` | `round_robin` | 从最高分目标中选择一个 |

每个 pipeline 阶段都通过名称选择算法。如果部署提供了其他路由实现，也可以在相同的 `routerPipeline` 字段中填写对应名称。

`kv_least_loaded` 优先考虑已确认的本地 KV 前缀位置，再选择负载较低的目标。`least_loaded` 忽略 KV 位置，只按当前请求负载评分。`uniform` 为所有候选项赋予相同分数；`round_robin` 会在同分目标之间按确定顺序轮转，`max` 则选择一个确定的同分目标。

只有模型、输入限制、请求能力和目标健康状态都兼容时，请求才会成为候选项。Router 会根据控制器发布的聚合或分离式拓扑选择目标。在 Prefill/Decode 和 Encoder/Prefill/Decode 拓扑中，各阶段选择必须位于控制器定义的同一 connector compatibility scope 中。一个 scope 可以包含每个阶段的多个 ModelGroup，不按 ordinal 固定配对。

KV 索引返回 `Unavailable` 时，目标仍可参与路由，但不获得 KV 前缀匹配优先权；路由仍会考虑其负载。位置查询和退化行为见 [KV 前缀索引](../kv-indexer/README_zh.md)。

编译进二进制的路由算法，以及 Filter、Scorer 和 Picker 的精确维护契约见 [Router 维护指南](MAINTAINER_zh.md)。
