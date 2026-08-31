# 自动扩缩容

自动扩缩容以完整 ModelGroup 为单位，不会独立调整 Pod、rank 或 E/P/D 成员数量。

```text
ScalingSnapshot
→ TriggerDecision
→ DesiredCapacity
→ ScalingAdjustment
→ ScalingDecision
```

- **Trigger** 判断当前观测是否可以进入评估。内置 `periodic` trigger 接受控制器轮询得到的每个完整、新鲜观测。
- **Decision** 计算期望容量。`queue` 对齐 Kubernetes HPA 的 `AverageValue` 语义；`queue_threshold` 在绝对积压边界上给出单 Group 调整建议。
- **Adjustment** 应用稳定窗口、硬容量边界和单轮 Group 变化限制。近期较高的容量建议会像 HPA stabilization window 一样延迟缩容。
- **Resolver** 负责生命周期约束。Group 转换未完成时保持当前容量，同时始终执行配置的最小和最大容量边界。

观测缺失、过期或不完整时保持当前容量，不会将其解释为零需求。自动扩缩容至少维持一个 Group，当前不支持 scale-to-zero。

## 队列需求

控制器组合两类具有独立 ownership 的信号：

```text
runtime 准备队列
+ max(后端分发队列, 推理调度器等待请求)
```

runtime 准备属于独立需求。后端分发与调度器指标可能短暂观察到同一请求，因此只计入两者较大的聚合值。存在 model-server 活跃请求时不会执行空闲缩容。

## 内置决策

### `queue`

```text
desiredGroups = ceil(queueRequests / targetAverageQueuedRequests)
```

队列为正时，平均值公式可以建议更高或更低容量。没有排队但仍有活跃请求时保持当前容量；完全空闲时建议容量为零，再由 `minGroups` 提供自动扩缩容的容量下限。

### `queue_threshold`

```text
queueRequests > scaleUpQueuedRequests
→ 建议 currentGroups + 1

queueRequests <= scaleDownQueuedRequests 且 activeRequests == 0
→ 建议 currentGroups - 1
```

该模式面向按服务总积压量而非每个 Group 平均队列进行配置的用户。

## 示例

```yaml
autoscaling:
  minGroups: 1
  maxGroups: 8
  pollingInterval: 5s
  observationMaxAge: 15s
  decision:
    algorithm: queue
    queue:
      targetAverageQueuedRequests: 1
  adjustment:
    algorithm: step
    scaleUp:
      stabilizationWindow: 0s
      maxGroupsPerEvaluation: 1
    scaleDown:
      stabilizationWindow: 300s
      maxGroupsPerEvaluation: 1
```

status 中的 `desiredGroups`、`adjustedGroups` 和 `appliedGroups` 分别表示 Decision、Adjustment 和生命周期解析后的容量。Trigger、Decision、Adjustment 与 Constraint 原因会分别发布。
