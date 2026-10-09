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

Each frontend replica maintains independent model admission state. `PreparedAdmissions::new` validates every selected algorithm through its factory and constructs independent candidates. Factories must not change active reservations, waiters, or metrics; an invalid candidate leaves the published rules unchanged. The runtime publisher calls `AdmissionRegistry::publish` only after accepting the prepared runtime.

The built-in `allow_all` and `concurrency` rules expose their shared work-unit state through `AdmissionRule::capacity_state`. Publication copies the candidate's limits into the active state and retains the active rule instance. Counts, FIFO waiters, and resource gauges survive the update; even unrestricted work returns counted permits so switching back to `concurrency` includes it. This hook is specific to rules whose entire decision is expressed by those shared limits, not a general algorithm replacement interface.

Lowering concurrency does not revoke running permits. Reducing or disabling the queue limits new arrivals, while existing waiters keep their original timeout. A previously queued batch larger than the new concurrency limit returns `AdmissionError::Closed` (HTTP 503), rather than `BatchTooLarge` (HTTP 400); dropping its queue reservation allows later waiters to proceed. Cancellation and timeout must likewise remove the waiter and release its queued count.

A new rule with different decision semantics should leave `capacity_state` at its default `None`. Changed custom rules stop accepting new requests, cancel waiting attempts through `close`, and activate their replacement only after accepted work releases its permits. New requests receive HTTP 503 during this handover; other models continue independently. `AdmissionRegistry::is_applied` remains false while any configured model is draining or has a pending replacement, so configuration acknowledgement reports actual activation rather than preparation.
