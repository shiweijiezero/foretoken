<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型服务自动扩缩容

[English](autoscaling.md) | [中文](autoscaling_zh.md)

在 `ModelService.spec.autoscaling` 中配置自动扩缩容。

## 最小配置

将以下配置加入 `ModelService`：

```yaml
spec:
  replicas: 1
  autoscaling:
    minReplicas: 1
    maxReplicas: 8
    decision:
      algorithm: queue
```

该配置每 5 秒评估一次队列负载，每次最多调整一个副本。未填写时，触发阶段默认使用 `periodic`，调整阶段默认使用 `step`。

需要暂停服务时，使用[停止与恢复命令](../cli/README_zh.md#停止与恢复)，扩缩容配置保持不变。

## 可选参数

每个阶段都使用 `algorithm` 和可选的 `parameters` 对象。省略参数时使用算法默认值。

| 阶段 | 算法 | 参数与默认值 |
| --- | --- | --- |
| Decision | `queue` | `targetAverageQueuedRequests: 1` |
| Decision | `queue_threshold` | `scaleUpQueuedRequests: 1`、`scaleDownQueuedRequests: 0` |
| Decision | `aimd` | `additiveIncrease: 1`、`multiplicativeDecreasePercent: 50`、`scaleUpQueuedRequests: 0` |
| Decision | `token_load` | `mode: throughput`；`latency` 使用较低阈值，`load` 使用自定义阈值 |
| Trigger | `periodic` | `interval: 5s` |
| Adjustment | `step` | `scaleUpStabilizationWindow: 0s`、`scaleDownStabilizationWindow: 300s` |
| Adjustment | `direct` | 不接受参数 |

例如，修改轮询间隔和缩容稳定窗口：

```yaml
trigger:
  algorithm: periodic
  parameters:
    interval: 10s
adjustment:
  algorithm: step
  parameters:
    scaleDownStabilizationWindow: 60s
```

未知算法或无效参数会使 `ModelService` 出现 `ScalingFailed` condition。

## Token 负载阈值

使用 `token_load` 根据 token 需求量扩缩容，适合请求长度差异较大的负载：

```yaml
decision:
  algorithm: token_load
```

默认的 `throughput` 模式允许积累更多工作；`latency` 使用较低阈值，更早触发扩容。通过 `decision.parameters.mode` 选择模式。

| 模式 | Prefill 扩容 / 缩容 | Decode 和 aggregate 扩容 / 缩容 |
| --- | --- | --- |
| `throughput` | `>= 1` / `< 0.1` | `> 1` / `< 0.6` |
| `latency` | `>= 0.1` / `<= 0` | `> 0.4` / `< 0.1` |

Prefill 使用排队输入 token 数与模型上下文长度的比值；decode 使用已调度及排队上下文 token 数与 KV token 容量的比值；聚合部署（aggregate）还计入排队输入 token。需求超过容量时，比值可以大于 1。

每个数据并行引擎独立评估。任一引擎满足扩容条件，即建议增加一个副本；减少副本则要求所有引擎满足缩容条件，且预计重新分配后的负载低于扩容阈值。

自定义阈值时选择 `load`，并填写全部四个参数：

```yaml
decision:
  algorithm: token_load
  parameters:
    mode: load
    prefillScaleUpQueueTokens: 8192
    prefillScaleDownQueueTokens: 0
    decodeScaleUpTokenRatio: 0.8
    decodeScaleDownTokenRatio: 0.6
```

此模式的 prefill 阈值是 token 数，decode 和 aggregate 阈值是 `[0, 1]` 范围内的比例。扩容阈值必须大于对应的缩容阈值，扩容使用 `>=`，缩容使用 `<=`。自定义阈值仅用于 `load` 模式。

Token 负载观测支持单一全注意力 KV 缓存组，且需关闭上下文并行（PCP/DCP=1）。编码器池，以及指标不可用、不完整或过期的目标，在配置的副本数上下限内保持当前容量。

## 使用 AIMD

使用以下配置选择 AIMD：

```yaml
decision:
  algorithm: aimd
```

当队列超过 `scaleUpQueuedRequests` 时，AIMD 增加 `additiveIncrease` 个副本；没有等待请求和活跃请求时，保留当前容量的 `multiplicativeDecreasePercent`。最终容量仍受调整算法以及服务最小、最大副本数限制。

## 查看自动扩缩容结果

结果发布在 `.status.autoscaling[]` 中：

```bash
kubectl get modelservice <name> -o json \
  | jq '.status.autoscaling[] | {
      id,
      direction,
      desiredReplicas: .decision.desiredReplicas,
      adjustedReplicas: .adjustment.adjustedReplicas,
      appliedReplicas,
      constraint: .constraint.reason
    }'
```

`desiredReplicas` 是算法建议，`adjustedReplicas` 包含调整规则的结果，`appliedReplicas` 是应用服务约束后实际写入的容量。

完整的维护示例见[多模型示例](../examples/multi-model-quickstart/README_zh.md)，实现边界见[自动扩缩容架构指南](../control-plane/internal/autoscaling/README_zh.md)。
