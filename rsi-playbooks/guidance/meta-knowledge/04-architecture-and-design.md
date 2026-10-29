<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 04 | Architecture and Method Design

English | [简体中文](04-architecture-and-design_zh.md)

Combine reference knowledge, source code, and performance analysis to develop your own algorithms, execution methods, and architecture. Refine them through implementation and experiments, then put them to use in a real service.

## Understand existing designs and technical principles

For the problem being optimized, study the computation, data layout, scheduling logic, and upstream and downstream calls. Understand why the current approach was chosen. Compare relevant research and upstream implementations to identify the trade-offs they make for their hardware, models, and workloads.

Establish the computational semantics and quality requirements the task must preserve, then reconsider choices in the existing implementation. Batch composition, parallel dimensions, data placement, stage boundaries, and synchronization may all admit other designs. Existing capabilities can be reused directly, and their implementations can also be improved for the current problem.

## Propose and compare implementations

Address the same problem at different layers, making clear where each change belongs and which part owns it:

- Operators and execution runtime: investigate computational algorithms, kernel fusion, data layout, memory-access reuse, compilation and graph execution, and coordination between host submission and device execution.
- Inference-engine internals: investigate request batching, token scheduling, prefill/decode execution, KV management, speculative decoding, model parallelism, and communication. The focus is how one inference instance organizes its work.
- Foretoken request-serving layer: investigate routing across instances, load balancing, cache-aware selection, queuing and admission, and request handoffs between stages. The focus is which requests go to which instance, and when.
- Foretoken cluster orchestration: investigate model instance counts, resource pools, autoscaling, stage ratios, and heterogeneous resource configurations, in coordination with Kubernetes node scheduling, resource allocation, and service lifecycle management.
- Cross-layer architecture: investigate how computation, state, and data transfer are organized across instances. Design engine execution, request routing, and resource orchestration together, with clear data, state, and control responsibilities at each layer.

Explain what each approach changes, where its gains come from, and what computation, storage, transfer, or coordination it adds. Estimate major costs using actual shapes and effective compute capacity and bandwidth, then implement focused pieces to resolve key unknowns. Revisit the trade-offs when batch sizes, context lengths, or hardware conditions change.

## Advance the design through focused experiments

Reuse real inputs and reference implementations from diagnosis so candidates run under the same conditions. Compare outputs, runtime state, and durations. Check whether the work expected to be saved actually decreases and where new costs arise. Use results to revise work partitioning, data layout, interfaces, and scheduling, rather than only tuning parameters of the same implementation.

First reuse Foretoken's deployment, evaluation, profiling, and recording capabilities, as well as suitable upstream experiment entry points. Only when they cannot answer the current question should you extract the relevant calls and necessary state to write a focused experiment or temporary tool. Preserve useful input construction and measurement methods for subsequent iterations.

After integrating an effective candidate into the model and service, continue analyzing full execution, including batch changes, paging or loop state, graph execution, parallel communication, cancellation, and resource release. If local gains diminish in the service, identify added conversions, scheduling, and waits, then improve the integration. Performance and quality under the task workload determine the approach's actual value.

Use available measurements and profiles to reassess the remaining costs after a gain. Capture another profile when the bottleneck has changed or attribution remains unclear, focusing it on that question.

## Refine approaches with multiple agents

Have different agents propose alternatives, analyze source code, estimate costs, or implement key pieces. Exchange views around concrete implementations: which work was eliminated, whether added costs offset the gains, whether the approach holds across workloads, and which other assumptions could change.

Use the results to refine the designs and validate retained changes together in the full service.
