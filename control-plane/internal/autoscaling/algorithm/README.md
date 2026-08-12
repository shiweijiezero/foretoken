# Autoscaling algorithm extensions

该目录统一承载 autoscaling 的无副作用扩展契约与算法实现：

```text
algorithm/
├── algorithm.go                 # Algorithm、Snapshot、Recommendation 契约
├── manual_algorithm.go          # manual
├── queue_algorithm.go           # queue
└── simple_step_algorithm.go     # 社区参考实现
```

算法：

- 接收一个 immutable `Snapshot`，目标是一个 Pool 或原子的 E/P/D scaling domain；
- 返回 fenced `Recommendation`，不能直接写 Kubernetes；
- 使用 `Apply`、`Hold`、`InsufficientData` 明确区分决策；
- 对同样的输入和配置必须确定性执行，时间判断只使用 `Snapshot.EvaluatedAt`；
- 不能持有 Kubernetes、telemetry、routing 或 credential client。

`core/` 独占 target identity、snapshot fencing、bounds、transition gate、rate limit、E/P/D 一致性、drain 和 Kubernetes reconciliation。稳定的可配置算法名称及静态装配位于 `../configuration.go` 和 `../autoscaler.go`。
