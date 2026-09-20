# 自动扩缩容架构

[English](README.md) | [中文](README_zh.md)

本包将控制器拥有的观测转换为 `ModelPool` 容量。用户通过 `ModelService.spec.autoscaling` 配置自动扩缩容；配置和状态使用方式见[自动扩缩容指南](../../../../docs/autoscaling_zh.md)。

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

每个 Trigger、Decision 和 Adjustment 阶段都拥有自己的编译期 descriptor 列表。新增算法时，在所属阶段添加实现和一个 descriptor；顶层 registry 负责构造选中的 factory，不接受运行时注册。算法各自负责参数默认值和语义校验，`core.DecodeParameters` 只提供共享的字段、类型解析，不把实现结构体直接暴露为配置。

三个阶段都接收可省略的 JSON 参数对象。Adjustment 构造函数还接收控制器持有的建议历史。Trigger 实现提供轮询间隔，控制器负责调度并据此推导观测有效期。容量上下限与生命周期约束仍属于平台职责。

控制器每轮只构造一次流水线。省略触发或调整阶段时，由组装入口选择 periodic 或 step；具体参数的默认值只由实现负责。新增实现需要重新构建并部署控制器，不采用运行时动态插件。

## 验证

修改本包后运行控制面验证：

```bash
make -C control-plane verify
```
