<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 请求路由

[English](README.md) | 简体中文

请求路由为请求选择模型、输入长度和能力要求都匹配的健康副本，也确保预填充/解码分离、编码/预填充/解码分离服务的各阶段相互兼容。

## 选择路由策略

优先选择等待请求较少的副本时，在 `FrontendService` YAML 中设置评分算法。以快速开始示例为例，修改 `examples/quickstart/qwen3-0.6b/frontend.yaml`：

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: queue_depth
```

重新部署修改后的配置：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

默认使用 `kv_least_loaded` 兼顾缓存复用和负载均衡，再由 `gamble_sampling` 选择目标。

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
| 按请求体中的 `session_id`，优先将同一会话的请求发往同一模型副本 | `session_affinity` |
| 优先选择加权请求长度较小的目标 | `throughput` |
| 按指定端点观测值排序 | `endpoint_attribute` |
| 所有目标得分相同 | `uniform` |

评分算法的选项放在 `scorer.parameters` 下。例如，让默认策略更偏向缓存复用：

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: kv_least_loaded
      parameters:
        cacheWeight: 0.7
```

`kv_least_loaded` 综合缓存复用与各 DP rank 的请求压力评分。默认给 GPU 本地前缀完整的缓存权重，超出本地前缀、需要从 CPU、文件或共享存储恢复的部分按一半计入。因此，共享缓存命中相同时，本地缓存更多的目标仍有优势；负载差异也能改变排名。

两个权重的取值范围均为 0 到 1：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `cacheWeight` | `0.5` | 权衡缓存与负载；设为 `0` 时只按负载评分，也不为该算法发起缓存查询。 |
| `restoreWeight` | `0.5` | 恢复部分相对于 GPU 本地复用的权重；`0` 只计本地复用，`1` 对两者同等计分。 |

在编码和解码阶段，`kv_least_loaded` 只按负载评分。这些权重表达路由偏好，不是缓存传输耗时的估计。

无法取得某个目标的前缀匹配结果时，该目标仍可参与路由，但不获得缓存偏好。支持的缓存及状态访问见 [KV 前缀索引](../kv-indexer/README_zh.md)。

## 根据得分选择目标

通过 `routerPipeline.picker.algorithm` 选择：

| 选择算法 | 行为 |
| --- | --- |
| `gamble_sampling`（默认） | 排名越高，选中概率越大；同分概率相同，低排名目标仍有机会被选中。 |
| `max` | 选择最高分目标。 |
| `power_of_two_choices` | 随机抽取两个不同目标，选择分数较高者，同分时随机选择。 |

## 按模型和执行阶段选择算法

在模型的 `ModelService` YAML 中设置 `spec.routerPipeline`，即可覆盖前端默认策略。例如，模型默认按负载选路，预填充阶段单独使用缓存感知策略：

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: least_loaded
    prefill:
      scorer:
        algorithm: kv_least_loaded
        parameters:
          cacheWeight: 0.7
    decode:
      scorer:
        algorithm: queue_depth
      picker:
        algorithm: max
```

`filter`、`scorer`、`picker` 分别确定最终选择：`encoder`、`prefill`、`decode` 的阶段配置优先于模型配置，模型配置优先于前端默认值。聚合推理使用模型级选择。删除某项配置即可恢复继承。

每个显式配置的项都需要指定 `algorithm`，并整体替换该算法的 `parameters`。省略参数时使用所选算法的默认值，不沿用上一级参数。

使用该部署原有的 `foretoken deploy` 命令应用修改。路由配置在线生效，不替换模型实例；配置无效时保留当前设置。同一前端下，多个 ModelService 对外提供相同模型名称时，最终路由配置需要一致。

## 查看路由表现

向服务发送流量时，在 [Grafana](../../../../observability/README_zh.md) 的路由选择区域比较选择份额、结果、可选目标和选择耗时。

请求限额与排队单独通过前端的[准入规则](../../README_zh.md#配置准入规则)配置。
