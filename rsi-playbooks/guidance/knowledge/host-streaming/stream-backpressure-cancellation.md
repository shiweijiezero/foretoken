<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Bound stream resource use through backpressure and cancellation

English | [简体中文](stream-backpressure-cancellation_zh.md) · [Knowledge](../README.md)

## Observations and Motivation

Regular engine token production can become bursty client delivery across decoded-event queues, response bodies, proxies, and transport windows. A successful enqueue or write only means the local layer accepted data. Without bounded buffering and cancellation ownership, slow or disconnected clients can retain host memory, engine work, and KV state that healthy requests need.

## Principles and Analysis

For queue occupancy B, producer rate p and consumer rate c, `dB/dt=p−c`. Under constant rates with p > c, a capacity K saturates after approximately `(K−B₀)/(p−c)`. Enlarging the buffer delays feedback but does not fix sustained mismatch. Each downstream queue can postpone pressure further.

SSE events are UTF-8 line frames dispatched at a blank line, not one token per packet. HTTP/2 has both stream and connection flow control. A buffering filter may require the whole response, while a streaming filter can forward incrementally. Disconnect, abort acceptance, engine termination, and KV release are separate events; generation at rate r for cancellation delay Δ wastes roughly rΔ tokens.

## Design

Keep response-stream ownership linked to a stable engine request identity. Let downstream demand control upstream polling or a bounded queue; an independent unbounded receiver breaks that pressure path. Transport flow control limits buffering, while engine cancellation ends resource use. A slow socket should not indefinitely block a shared device scheduler.

Heartbeat comments preserve connection liveness without resetting request deadlines or backend idle time. Foretoken's cancellable output stream, model-server abort exchange, and idle cleanup provide these ownership boundaries.

## Implementation

The application-side state flow is:

```text
while request is active:
    select downstream readiness, disconnect, deadline, backend idle
    if downstream ready:
        poll upstream; frame event; await response write
        keep disconnect/deadline/backend-idle cancellation armed during awaits
    if disconnected or timed out:
        stop polling; request engine abort using request identity
        terminate the response and cancel owned forwarding tasks
        break
engine scheduler observes abort:
    exclude request from future batches
    retire in-flight work; release request state/KV when safe
```

If producer and writer are separate tasks, use a bounded channel and make sender/receiver closure cancel the owned path. The engine's abort acknowledgement is not a device-completion fence. Already submitted kernels may finish before safe cleanup. Envoy's read suppression and buffer limits propagate pressure through the proxy; whole-body filters change this behavior. SSE parsing must accumulate complete events across arbitrary network reads.

## Impact and Results

Bounded queues cap local host buffering and expose slow consumers. Cancellation removes future request work and eventually releases KV rather than merely closing HTTP. These mechanisms preserve capacity for healthy requests during disconnects. They add coordination and may leave a final in-flight step; transport pressure alone does not establish that generation has stopped. Connection-level HTTP/2 pressure can affect other streams sharing that connection.

## References

- [WHATWG SSE specification](https://html.spec.whatwg.org/multipage/server-sent-events.html): UTF-8, line parsing, blank-line dispatch, comments, and reconnection. EventSource reconnection is not automatic request replay for every LLM HTTP client.
- [Envoy flow-control documentation](https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/flow_control): listener/cluster/stream limits and buffering filters.
- [Foretoken shared stream handling](../../../../data-plane/frontend/src/server/src/api/stream.rs) and [HTTP model facade](../../../../data-plane/frontend/src/llm-facade/src/http.rs): at `e6ae3df6`; heartbeat, idle cleanup, and accepted-abort versus final-termination ownership.
- [Foretoken metric definitions](../../../../benchmarks/docs/metrics.md): received chunk intervals and successful-request timing, which do not expose backend resource-release time.
