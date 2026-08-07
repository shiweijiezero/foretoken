# Simple Router Policy

该目录用于放置可供用户参考和扩展的 Router 算法，与 `builtins/` 中 Foretoken 提供的基础实现分开。当前示例组合：

```text
SimpleFilter → SimpleScorer → SimplePicker
```

- `SimpleFilter`：按允许的完整路由拓扑过滤；不负责流控、容量或 reservation。
- `SimpleScorer`：保留拓扑优先级，并偏好总负载更低的选项。
- `SimplePicker`：按分数排序，使用 backend ID 提供确定性同分排序。
- `simple_policy(...)`：将三者组装成可注入 `PolicyRouter` 的 `RouterPolicy`。

示例：

```rust
use foretoken_router::{RouteOptionKind, algorithm::simple_policy};

let policy = simple_policy([
    RouteOptionKind::Aggregate,
    RouteOptionKind::PrefillDecode,
    RouteOptionKind::EncoderPrefillDecode,
]);
```

扩展只能缩小或排序 Router 核心已经验证的完整 option。后端健康、capability、同域 E/P/D 拓扑、容量复查、原子 reservation 和释放仍由 Router 核心负责。

流控、排队、公平性和 admission 后续应通过独立阶段扩展，不应塞入 `RouteFilter`。
