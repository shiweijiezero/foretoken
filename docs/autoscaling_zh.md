<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型服务自动扩缩容

[English](autoscaling.md) | [中文](autoscaling_zh.md)

自动扩缩容根据请求负载调整 `ModelService` 容量。先在服务配置中启用自动扩缩容，再在工作负载运行时查看服务状态。

## 容量单位

聚合模型服务的每个副本按照配置的资源和并行参数运行完整模型。对于将编码、预填充和解码分开运行的 E/P/D 服务，一个副本包含 encoder、prefill 和 decode 三个阶段，三者一起扩缩；所需资源为各阶段配置的资源之和。

`spec.replicas` 提供基线容量。配置 `autoscaling` 后，`minReplicas` 和 `maxReplicas` 从首次协调起就约束实际创建的容量。

## 配置队列自动扩缩容

在已有 `ModelService` 中添加以下 `spec` 配置。它从 1 个副本开始，在 1–8 个副本之间运行，每 5 秒评估一次近期队列负载，并且每次最多调整 1 个副本：

```yaml
spec:
  replicas: 1
  autoscaling:
    minReplicas: 1
    maxReplicas: 8
    decision:
      algorithm: queue
```

`periodic` 按配置的间隔评估队列负载。指标缺失、过期或不完整时，保持当前容量。自动扩缩容至少保留一个副本。

`queue` 根据每个副本的平均等待请求数计算容量。`queue_threshold` 则在配置的服务总积压边界按一次一个副本调整容量。`direct` 在应用最小和最大副本数限制后直接应用建议；`step` 每次评估最多调整一个副本，并可分别配置扩容和缩容稳定窗口。

缩容稳定窗口使用当前控制器进程保存的近期建议。控制器重启或 leader 切换不会保留这些历史，因此可能缩短等待缩容的延迟。

## 算法参数

三个阶段都使用 `algorithm` 和可选的 `parameters`。省略参数时使用所选算法的默认值；整个 trigger 或 adjustment 阶段省略时，分别使用 `periodic` 和 `step`。算法构造函数在写入容量前拒绝未知字段、错误类型和无效值。新增算法编译进控制器后，用户只需在服务配置中选择它，无需修改 CRD。

| 算法 | 参数 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `queue` | `targetAverageQueuedRequests` | `1` | 正整数 |
| `queue_threshold` | `scaleUpQueuedRequests` | `1` | 非负整数 |
| `queue_threshold` | `scaleDownQueuedRequests` | `0` | 非负整数，不超过 `scaleUpQueuedRequests` |
| `aimd` | `additiveIncrease` | `1` | 1–2147483647 的整数 |
| `aimd` | `multiplicativeDecreasePercent` | `50` | 1–99 的整数；空闲时保留的容量百分比 |
| `aimd` | `scaleUpQueuedRequests` | `0` | 非负整数 |
| `periodic`（trigger） | `interval` | `5s` | 正的时间长度 |
| `step`（adjustment） | `scaleUpStabilizationWindow` | `0s` | 非负时间长度 |
| `step`（adjustment） | `scaleDownStabilizationWindow` | `300s` | 非负时间长度 |
| `direct`（adjustment） | 无 | — | 不接受参数 |

控制器必须包含对应名称的算法。未知算法名称或无效参数会使 ModelService 出现 `ScalingFailed` condition；Kubernetes 校验参数必须为对象，由选中的算法校验对象内容。

例如，在已有的 `autoscaling` 中覆盖轮询间隔和缩容稳定窗口：

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

## 使用加法扩容与空闲时的乘法缩容

将已有 autoscaling 中的决策块替换为以下配置，即可选择 AIMD：

```yaml
decision:
  algorithm: aimd
```

等待请求总量超过 `scaleUpQueuedRequests` 时，AIMD 在当前已请求容量上增加 `additiveIncrease`。等待请求和活跃请求都为零时，保留当前容量的 `multiplicativeDecreasePercent` 百分比，向下取整；其他情况保持容量。例如，当前 5 个副本完全空闲，按默认的 50% 保留比例，建议容量为 2。

建议仍需经过所选的调整策略和生命周期约束。默认 `step` 每次常规评估最多增减一个副本，并保留缩容稳定窗口；如果希望在最小／最大容量和转换约束内直接采用完整的 AIMD 建议，可选择 `direct`。

## 查看扩缩容决策

自动扩缩容结果发布在 `.status.autoscaling[]` 中，每个扩缩目标对应一项。使用以下命令查询维护中的多模型示例：

```bash
kubectl get modelservice multi-model-qwen3-0.6b \
  --namespace foretoken-multi-model-demo \
  -o json | jq '.status.autoscaling[] | {
    id,
    kind,
    role,
    observationState,
    direction,
    desiredReplicas: .decision.desiredReplicas,
    adjustedReplicas: .adjustment.adjustedReplicas,
    appliedReplicas,
    constraint: .constraint.reason
  }'
```

`desiredReplicas` 是算法建议，`adjustedReplicas` 是稳定窗口和速率限制后的结果，`appliedReplicas` 是生命周期与最小/最大副本数限制后写入目标的容量。`observationState`、各阶段 reason 和 `constraint` 用于说明容量为何保持或改变。

聚合模型服务的 `kind` 为 `Pool`。E/P/D 服务的 `kind` 为 `EPDPipelineScope`，`role` 为 `EPD`。

## 使用维护中的示例

[多模型示例](../examples/multi-model-quickstart/README_zh.md)部署一个按队列自动扩缩的 Qwen 服务和一个固定容量的 Llama 服务，其中包含有界并发负载和观察容量变化的状态命令。

## 迁移旧版配置

升级前保存现有服务配置，并将算法专属值移入各自阶段的 `parameters` 对象：

| 旧字段 | 新字段 |
| --- | --- |
| `decision.queue.*` / `decision.queueThreshold.*` | `decision.parameters.*` |
| `trigger.interval` | `trigger.parameters.interval` |
| `adjustment.scaleUp.stabilizationWindow` | `adjustment.parameters.scaleUpStabilizationWindow` |
| `adjustment.scaleDown.stabilizationWindow` | `adjustment.parameters.scaleDownStabilizationWindow` |

删除旧字段；与默认值相同的参数可以省略。控制器和 CRD 需配套升级，并在新控制器开始协调已有服务前提交迁移后的配置。新 schema 不保留旧字段，未迁移的设置可能丢失并被算法默认值替代。回退时需一并恢复旧控制器、CRD 和保存的服务配置。

## 维护者架构

控制器阶段、观测聚合、算法扩展边界和生命周期解析见[自动扩缩容维护者 README](../control-plane/internal/autoscaling/README_zh.md)。
