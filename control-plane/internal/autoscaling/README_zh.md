# 自动扩缩容

自动扩缩容以完整 ModelGroup 为单位，不会独立调整 Pod、rank 或 E/P/D Group 的成员数量。

Controller 首先判断是否需要根据当前观测进行评估。Decision algorithm 随后计算 `DesiredCapacity`，即需要的完整 Group 数。Adjustment algorithm 应用容量边界和单轮调整限制，core 生命周期规则再生成最终 `ScalingDecision`。最后由 ModelService controller 将实际应用的容量写入 `ModelPool.spec.desiredGroups`。

```text
ScalingSnapshot
→ TriggerDecision
→ DesiredCapacity
→ ScalingAdjustment
→ ScalingDecision
```

Trigger 可以在计算 `DesiredCapacity` 前保持当前容量。
观测缺失、过期或不完整时同样保持当前容量，不会将其解释为零负载。

## 输出示例

假设一个 Pool 当前请求 2 个 Group，其中 1 个 Group 可路由，队列中有 5 个等待请求。内置 queue algorithm 计算需要增加 1 个完整 Group，step adjustment 允许本轮执行该调整：

```text
ScalingSnapshot:
  target:
    kind: Pool
    name: aggregate
    uid: 8c88ee9a-c10f-41fd-98ef-a09d256b5213
  capacity.requestedGroups: 2
  capacity.routableGroups: 1
  observation.queueRequests: 5
  limits: [1, 8]

TriggerDecision:
  disposition: Fire
  reason: Periodic

DesiredCapacity:
  disposition: Apply
  groups: 3
  reason: QueuePressure

ScalingAdjustment:
  adjustedGroups: 3
  reason: StepUp

ScalingDecision:
  target:
    kind: Pool
    name: aggregate
    uid: 8c88ee9a-c10f-41fd-98ef-a09d256b5213
  appliedGroups: 3
  direction: Up

ModelPool[aggregate].spec.desiredGroups: 2 → 3
```

`DesiredCapacity.groups` 是根据负载计算的容量；`adjustedGroups` 和 `appliedGroups` 分别表示经过调整规则和生命周期规则后，本轮实际采用的容量。

```text
autoscaling/
├── core/       # 固定输入、接口、流水线和结果规则
├── algorithm/  # 可替换的 trigger、decision 和 adjustment algorithms
└── tests/      # 自动扩缩容公共行为测试
```

实现使用稳定的 lower-snake-case 名称注册。名称为空、重复、未知或配置无效时都会返回明确错误。
