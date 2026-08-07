<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Router

[中文](README_zh.md)

`foretoken-router` selects a backend execution plan for each request. The core
router checks correctness and capacity. Routing algorithms only rank or filter
valid plans.

## How routing works

`PolicyRouter::select`:

1. filters backends by model, revision, input length, health, and capabilities;
2. builds valid `Aggregate`, `Prefill + Decode`, or
   `Encoder + Prefill + Decode` plans;
3. removes plans without available capacity;
4. applies the configured filter, scorer, and picker;
5. reserves capacity for the selected plan.

An algorithm can read:

- request model, revision, capabilities, and token data;
- plan topology;
- backend ID, role, domain, and current load;
- whether a backend supports KV-prefix locality scoring.

The core router still owns topology checks, health checks, admission, and
capacity reservations.

## Built-in algorithms

Set `FORETOKEN_ROUTER_ALGORITHM` to one of these values:

| Value | Behavior |
| --- | --- |
| `kv_aware` | Prefer topology, then KV-prefix locality, then lower load. Default. |
| `least_loaded` | Prefer topology, then lower load. |
| `round_robin` | Prefer topology and rotate exact ties. |

`RouteScore` uses lexicographic ordering. Larger values win.

## Add a built-in algorithm

Use this path when users should select the algorithm through
`FORETOKEN_ROUTER_ALGORITHM`.

1. Add a variant to `RouterAlgorithm` in `src/builtins/algorithm.rs`.
2. Add its `snake_case` name to `as_str`, `FromStr`, and `RouterAlgorithm::ALL`.
3. Add the concrete implementation under `src/builtins/filter`, `scorer`, or `picker`.
4. Build the policy in `RouterAlgorithm::policy`.
5. Add tests under `src/tests/`.
6. Update both README files.

Example scorer:

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

Keep scoring deterministic and fast. Use saturating arithmetic and checked
integer conversion for telemetry values.

## Build a custom policy

Use `PolicyRouter::with_policy` when the algorithm does not need a built-in
configuration name:

```rust
let policy = RouterPolicy::new(
    Arc::new(MyFilter),
    Arc::new(MyScorer),
    Arc::new(MyPicker),
);
let router = PolicyRouter::with_policy(inventory, policy);
```

Choose the smallest extension point:

- `RouteFilter`: reject an otherwise valid plan;
- `RouteScorer`: score each plan;
- `RoutePicker`: order scored plans and handle ties.

A picker cannot remove plans. The core appends any plan that the picker omits.
Use a filter when a plan must be rejected.

KV locality is a soft hint. Missing KV data should return a neutral score, not
reject a valid plan.

## Keep these rules in the core

Algorithms must not take ownership of:

- model, revision, capability, readiness, or input-length checks;
- topology construction;
- capacity checks and reservations;
- request mutation;
- plan execution.

The `Router` trait is sealed so every algorithm uses the same correctness and
capacity rules.

## Test

Run from the repository root:

```bash
./scripts/bootstrap-vllm-rust.sh
cargo fmt --manifest-path data-plane/frontend/Cargo.toml --all -- --check
cargo test --manifest-path data-plane/frontend/Cargo.toml -p foretoken-router --locked
```
