<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Router

[English](README.md)

`foretoken-router` 为每个请求选择后端执行计划。核心 Router 负责检查正确性和容量。路由算法只负责过滤或排序合法计划。

## 路由流程

`PolicyRouter::select` 会：

1. 根据 model、revision、输入长度、健康状态和 capabilities 过滤后端；
2. 构造合法的 `Aggregate`、`Prefill + Decode` 或
   `Encoder + Prefill + Decode` 计划；
3. 移除没有可用容量的计划；
4. 应用 filter、scorer 和 picker；
5. 为最终计划预留容量。

算法可以读取：

- 请求的 model、revision、capabilities 和 token 信息；
- 计划的拓扑；
- 后端的 ID、role、domain 和当前负载；
- 后端是否支持 KV 前缀局部性评分。

拓扑检查、健康检查、准入和容量预留仍由核心 Router 负责。

## 内置算法

通过 `FORETOKEN_ROUTER_ALGORITHM` 选择：

| 配置值 | 行为 |
| --- | --- |
| `kv_aware` | 依次偏好拓扑、KV 前缀局部性和较低负载。默认算法。 |
| `least_loaded` | 依次偏好拓扑和较低负载。 |
| `round_robin` | 优先考虑拓扑，并在完全同分时轮转。 |

`RouteScore` 按字段顺序比较。分数越大，优先级越高。

## 添加内置算法

如果用户需要通过 `FORETOKEN_ROUTER_ALGORITHM` 选择算法，请按以下步骤添加：

1. 在 `src/builtins/algorithm.rs` 的 `RouterAlgorithm` 中增加 variant。
2. 在 `as_str`、`FromStr` 和 `RouterAlgorithm::ALL` 中加入它的 `snake_case` 名称。
3. 将具体实现放入 `src/builtins/filter`、`scorer` 或 `picker`。
4. 在 `RouterAlgorithm::policy` 中组装策略。
5. 在 `src/tests/` 中添加测试。
6. 同时更新中英文 README。

Scorer 示例：

```rust
struct MyScorer;

impl RouteScorer for MyScorer {
    fn score(
        &self,
        option: &RouteOptionCandidate,
        _context: RouteContext<'_>,
    ) -> RouteScore {
        RouteScore {
            topology: topology_score(option.kind),
            locality: 0,
            load: -total_load(option),
        }
    }
}
```

评分逻辑应简单、稳定、快速。处理 telemetry 数值时，应使用饱和运算和受检查的整数转换。

## 组合自定义策略

如果算法不需要内置配置名称，可以使用 `PolicyRouter::with_policy`：

```rust
let policy = RouterPolicy::new(
    Arc::new(MyFilter),
    Arc::new(MyScorer),
    Arc::new(MyPicker),
);
let router = PolicyRouter::with_policy(inventory, policy);
```

请选择职责最小的扩展点：

- `RouteFilter`：拒绝某个原本合法的计划；
- `RouteScorer`：为每个计划评分；
- `RoutePicker`：排列计划并处理同分情况。

Picker 不能删除计划。核心 Router 会补回 picker 遗漏的计划。如果必须拒绝某个计划，请使用 filter。

KV locality 只是软提示。KV 数据缺失时应返回中性分数，不能拒绝合法计划。

## 核心职责

算法不能接管以下职责：

- model、revision、capability、readiness 和输入长度检查；
- 拓扑构造；
- 容量检查和预留；
- 修改请求；
- 执行计划。

`Router` trait 被设计为 sealed，确保所有算法使用相同的正确性和容量规则。

## 测试

在仓库根目录运行：

```bash
./scripts/bootstrap-vllm-rust.sh
cargo fmt --manifest-path data-plane/frontend/Cargo.toml --all -- --check
cargo test --manifest-path data-plane/frontend/Cargo.toml -p foretoken-router --locked
```
