<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Implementing admission rules

English | [简体中文](admission-rules_zh.md)

To add an admission rule, start from [allow_all](../../data-plane/frontend/src/admission/src/algorithm/allow_all.rs), or use [concurrency](../../data-plane/frontend/src/admission/src/algorithm/concurrency.rs) for an example with queuing and resource reservations. To configure an existing rule, see [Frontend admission](../../data-plane/frontend/README.md#configure-admission-rules).

## Make the decision

Implement `AdmissionRule::admit`:

```rust
async fn admit(
    &self,
    request: &AdmissionRequest,
    context: &AdmissionContext<'_>,
) -> Result<AdmissionPermit, AdmissionError>;
```

Use `request` for input summaries and output budgets, and `context` for the deadline and current model observations. Field definitions are in [AdmissionRequest](../../data-plane/frontend/src/admission/src/request.rs) and [AdmissionContext](../../data-plane/frontend/src/admission/src/context.rs).

Return a permit to accept the request or an error to reject it. The rule may wait; the framework handles deadline expiry and caller cancellation. While queued, hold `context.queue.begin_wait()`'s guard to record the wait.

For resource-free admission, return `AdmissionPermit::default()`. Otherwise, return `AdmissionPermit::new(reservation)`: the reservation must already own the capacity, release it on drop, and transfer one candidate's share through `split_one()`.

Update `context.metrics.active` and `context.metrics.queued` only for work units actually reserved or queued by the algorithm. Release the corresponding count with its reservation; splitting transfers already-counted units without incrementing them again. The framework records call results and queue timing separately.

## Register the rule

Provide `from_parameters(Value) -> Result<Self, String>` to validate parameters and construct a model's rule. Add it to `declare_admission_algorithms!` in [algorithm/mod.rs](../../data-plane/frontend/src/admission/src/algorithm/mod.rs); external implementations can register an `AdmissionDescriptor` through `inventory`.

Expose new configuration through the shared FrontendService and ModelService admission API and regenerate the CRDs. Optional `capacity`, `requires_ready_runtime`, and `close` methods are documented on [AdmissionRule](../../data-plane/frontend/src/admission/src/lib.rs). `close` must wake algorithm-owned waiters without revoking accepted reservations.

## Configuration lifecycle

Each frontend replica maintains independent rule instances and queues for its models; routing changes retain unchanged rules. Factories validate and construct rules without publishing metrics or changing active requests. The framework activates metrics when the configuration takes effect.

A changed model rule stops accepting new requests, cancels waiting attempts, and lets accepted work finish before activating its replacement. New requests receive HTTP 503 during this handover; other models continue independently.
