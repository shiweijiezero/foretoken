<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型服务自动扩缩容

[English](autoscaling.md) | [中文](autoscaling_zh.md)

在 `ModelService.spec.autoscaling` 中配置自动扩缩容。Controller 使用已编译的内置算法，用户不需要修改 CRD 或注册算法。

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

## 可选参数

每个阶段都使用 `algorithm` 和可选的 `parameters` 对象。省略参数时使用算法默认值。

| 阶段 | 算法 | 参数与默认值 |
| --- | --- | --- |
| Decision | `queue` | `targetAverageQueuedRequests: 1` |
| Decision | `queue_threshold` | `scaleUpQueuedRequests: 1`、`scaleDownQueuedRequests: 0` |
| Decision | `aimd` | `additiveIncrease: 1`、`multiplicativeDecreasePercent: 50`、`scaleUpQueuedRequests: 0` |
| Decision | `dynamo_load` | `mode: throughput`，prefill 队列阈值 `1/0`，decode KV cache 阈值 `0.8/0.6` |
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

## Dynamo 反应式负载扩缩容

使用 `dynamo_load` 选择兼容 Dynamo 的反应式策略：

```yaml
decision:
  algorithm: dynamo_load
  parameters:
    mode: throughput
```

Aggregate、encoder 和 prefill Pool 使用排队请求数；decode Pool 在模型服务
提供该指标时使用 KV cache 利用率。`latency` 模式使用更低的 decode 阈值（扩容
`0.4`、缩容 `0.1`）。decode 所需指标不可用时算法会保持当前容量，不会把缺失
指标解释为零。已有的触发器、step 调整、上下限和生命周期约束仍然生效。

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
