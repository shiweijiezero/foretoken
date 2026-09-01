# 自动扩缩容

用户通过公共 API 调整模型服务的副本数。控制器内部，一个服务副本对应一个完整的 ModelGroup；ModelGroup 可以包含多个 Pod、rank 或 E/P/D 成员，这些成员始终作为一个整体扩缩。

```text
ScalingSnapshot
→ TriggerDecision
→ ReplicaRecommendation
→ ReplicaAdjustment
→ ScalingDecision
```

扩缩容分为四个职责清晰的阶段：

- **触发阶段（Trigger）**：判断当前观测是否完整、有效，可以用于本轮评估。内置 `periodic` 算法按配置的时间间隔执行。
- **容量决策阶段（Decision）**：根据请求积压计算期望副本数。`queue` 采用 Kubernetes HPA 的平均值目标语义；`queue_threshold` 根据服务的总积压量给出增减一个副本的建议。
- **调整阶段（Adjustment）**：决定是否立即应用建议，或者通过稳定窗口延迟扩缩。`step` 每次最多增减一个副本。
- **生命周期解析阶段（Resolver）**：执行最小和最大副本数限制，并在 ModelGroup 仍处于创建、更新或排空状态时保持当前容量。

`trigger` 可以省略，默认每 5 秒执行一次 `periodic` 评估。控制器只接受最近三个轮询周期内采集的指标，因此默认有效期为 15 秒。指标缺失、过期或不完整时，控制器会保持当前副本数，不会把它们当成零负载。

自动扩缩容至少保留一个副本，当前不支持缩容到零。

## 请求积压量

控制器组合两类来源明确的等待请求：

```text
等待模型运行环境准备的请求数
+ max(等待后端开始响应的请求数, 推理调度器中的等待请求数)
```

等待模型运行环境准备的请求属于独立需求。后端分发阶段和推理调度器可能短暂观察到同一个请求，因此两者只取较大值，避免重复计数。模型服务器仍有正在处理的请求时，不会按完全空闲状态缩容。

## 内置容量决策算法

### `queue`

```text
desiredReplicas = ceil(queueRequests / targetAverageQueuedRequests)
```

该算法按每个副本期望承担的平均等待请求数计算容量，因此既可以建议扩容，也可以建议缩容。没有等待请求但仍有正在处理的请求时，保持当前副本数；服务完全空闲时建议容量为零，再由 `minReplicas` 保证最小副本数。

### `queue_threshold`

```text
queueRequests > scaleUpQueuedRequests
→ 建议 currentReplicas + 1

queueRequests <= scaleDownQueuedRequests 且 activeRequests == 0
→ 建议 currentReplicas - 1
```

该算法适合希望直接按服务总积压量设置扩缩边界的用户。

## 内置调整算法

- `direct`：将容量决策裁剪到 `minReplicas` 和 `maxReplicas` 范围后立即应用，不使用稳定窗口。
- `step`：每次触发最多增减一个副本，并可分别配置扩容和缩容稳定窗口。

## 配置示例

顶层 `spec.replicas` 设置服务启动时的初始副本数。服务启动后，自动扩缩容在 `minReplicas` 和 `maxReplicas` 范围内接管副本数。

```yaml
autoscaling:
  minReplicas: 1
  maxReplicas: 8
  trigger:
    algorithm: periodic
    interval: 5s
  decision:
    algorithm: queue
    queue:
      targetAverageQueuedRequests: 1
  adjustment:
    algorithm: step
    scaleUp:
      stabilizationWindow: 0s
    scaleDown:
      stabilizationWindow: 300s
```

状态中的 `desiredReplicas`、`adjustedReplicas` 和 `appliedReplicas` 分别表示容量决策、调整阶段和生命周期解析后的副本数。触发、容量决策、调整和生命周期约束的原因会分别记录，便于排查扩缩行为。
