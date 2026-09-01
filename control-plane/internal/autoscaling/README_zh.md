# 自动扩缩容架构

[English](README.md) | [中文](README_zh.md)

本包将控制器拥有的观测转换为 `ModelPool` 容量。用户通过 `ModelService.spec.autoscaling` 配置自动扩缩容；配置和状态使用方式见[自动扩缩容指南](../../../docs/autoscaling_zh.md)。

## 职责归属

`ModelService` 控制器负责调度、观测采集、扩缩目标发现、状态发布，以及将容量写入 `ModelPool`。算法保持无副作用：只评估一个完整观测并返回容量建议。

聚合目标扩缩一个 Pool。E/P/D 目标扩缩一个 `EPDPipelineScope`，将相同容量写入 encoder、prefill 和 decode Pool。

## 评估流水线

```text
控制器轮询
→ ScalingSnapshot
→ TriggerDecision
→ ReplicaRecommendation
→ ReplicaAdjustment
→ ScalingDecision
→ ModelPool 容量和 ModelService 状态
```

控制器向流水线提供完整、近期的观测。`periodic` 接受这些观测，不拥有时间间隔或重新入队循环。即使缺少观测，Resolver 仍会应用最小和最大副本数硬限制；目标处于转换中时保持容量。

`step` 的稳定窗口使用当前控制器进程保存的近期建议。历史刻意保存在运行时本地，因此重启或 leader 切换不会恢复尚未结束的缩容延迟。

## 扩展边界

内置算法位于 `algorithm/`。Trigger、Decision 和 Adjustment 实现返回领域结果，不读取 Kubernetes 资源、不修改容量，也不调度工作。新增实现只有在它代表当前独立负责的建议策略时才有意义；控制器生命周期行为保留在 `core` 和 ModelService reconciler 中。

修改本包时，同步核对用户可见算法名称、默认值、校验、状态 reason 和自动扩缩容指南。

## 验证

修改本包后运行控制面验证：

```bash
make -C control-plane verify
```
