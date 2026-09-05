<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 模型服务自动扩缩容

[English](autoscaling.md) | [中文](autoscaling_zh.md)

自动扩缩容根据请求负载调整 `ModelService` 容量。先在服务配置中启用自动扩缩容，再在工作负载运行时查看服务状态。

## 容量单位

对于聚合模型服务，一个副本对应一个完整的 `ModelGroup`。对于 E/P/D 服务，一个副本对应一起扩缩的 encoder、prefill 和 decode 三元组。资源请求需要覆盖完整容量单位：一个 E/P/D 副本会消耗三个 Group 的资源。

`spec.replicas` 提供基线容量。配置 `autoscaling` 后，`minReplicas` 和 `maxReplicas` 从首次协调起就约束实际创建的容量。

## 配置队列自动扩缩容

在已有 `ModelService` 中添加以下 `spec` 配置。它从 1 个副本开始，在 1–8 个副本之间运行，每 5 秒评估一次近期队列负载，并且每次最多调整 1 个副本：

```yaml
spec:
  replicas: 1
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

控制器负责评估调度。`periodic` 会评估控制器提供的每个完整、近期观测，不是算法内部的定时器。指标缺失、过期或不完整时，控制器保持当前容量。自动扩缩容至少保留一个副本。

`queue` 根据每个副本的平均等待请求数计算容量。`queue_threshold` 则在配置的服务总积压边界按一次一个副本调整容量。`direct` 在应用最小和最大副本数限制后直接应用建议；`step` 每次评估最多调整一个副本，并可分别配置扩容和缩容稳定窗口。

缩容稳定窗口使用当前控制器进程保存的近期建议。控制器重启或 leader 切换不会保留这些历史，因此可能缩短等待缩容的延迟。

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

## 维护者架构

控制器阶段、观测聚合、算法扩展边界和生命周期解析见[自动扩缩容维护者 README](../control-plane/internal/autoscaling/README_zh.md)。
