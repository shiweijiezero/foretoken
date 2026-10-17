<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Anticipate capacity demand through startup-aware token forecasting

English | [简体中文](startup-aware-scaling_zh.md)

## Observations and Motivation

Requested capacity can become useful minutes after a decision. Scraping, smoothing, scheduling, image/checkpoint loading, graph warmup and route publication all contribute. Predicting only the next 15-second sample is structurally late when startup takes 180 seconds; demand must be projected to readiness time.

## Principles and Analysis

For token demand x in tokens/s sampled every T, smooth `x̂_t = α*x_t + (1−α)*x̂_(t−T)`. The decomposition paper projects over startup Δ:

`x̂_(t+Δ) = x̂_t + slope*(Δ−T/2)`, with `slope = (x̂_t − x̂_(t−T))/T`.

Δ and T share time units; α is dimensionless. The half-interval correction centers the slope between observations. Add `β*min(σ, γ*abs(x̂_(t+Δ)))`, where σ is forecast-error standard deviation, β scales uncertainty and γ caps it relative to demand. Apply these calculations separately to prefill and decode.

A forecast can anticipate an observable ramp, not an unannounced jump. A warm capacity floor or admission policy handles that uncertainty at a real cost. Checkpoint storage, CPU deserialization, quantization and graphs change startup separately from steady-state throughput.

## Design

Track requested, starting, ready, routable and draining capacity separately. Two routable replicas plus two starting replicas provide two current service units and four committed replicas. Starting capacity reduces duplicate provisioning, not current queueing; failed/unschedulable starts differ from normal warmup.

The controller prepares lifecycle observations and owns actuation; forecasting returns a recommendation through the algorithm boundary. Foretoken holds transitioning targets and keeps stabilization history in the current controller process, but its public policies do not implement demand forecasting.

## Implementation

1. Collect fresh demand and lifecycle observations, including startup-to-routability delay for the relevant configuration.
2. Smooth each stage's work, compute the trend and project it to the measured readiness horizon.
3. Add bounded forecast uncertainty and divide projected work by calibrated stage capacity to obtain a replica target.
4. Account for pending commitments without crediting them as immediate throughput; exclude failed starts from normal readiness expectations.
5. Return the target for controller bounds/stabilization and provisioning. Reconcile actual readiness and forecast errors on subsequent observations.

Dynamo supplies constant/ARIMA/Prophet/Kalman `predict_next` interfaces and pending-start handling; a one-step interface alone does not establish startup-horizon prediction. AIBrix's cited predictive fields are accepted/validated but not consumed by its controller, whose implementation remains reactive.

## Impact and Results

The paper's controlled hardware comparison uses Qwen2.5-7B-Instruct, vLLM 0.11 and one A100 40GB per replica, a five-minute 5→15→5 QPS trace, 256–4096-token Pareto inputs and maximum output 256. With an injected 60-second startup delay and a 2-second TTFT target, reactive KEDA reports 63.5% violations and 24.0-s P95 TTFT; EWMA lookahead plus bounded uncertainty reports 3.7% and 1.6 s. Violations use completed requests as the denominator.

This physical comparison forecasts running-plus-waiting request demand, not separate token streams, and establishes the delay-aware provisioning effect. Precision is unspecified; results describe one controlled trace, not the five-seed simulation. Earlier provisioning costs additional committed capacity rather than accelerating kernels.

## References

- [Decomposing Predictive Kubernetes Autoscaling](https://arxiv.org/html/2609.20874v1)
- [Dynamo predictors](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/core/load/predictors.py)
- [Dynamo throughput scaler](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/core/throughput_scaling.py)
- [AIBrix predictive documentation](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/docs/source/features/scaling-performance/autoscaling/predictive-autoscaling.rst)
- [AIBrix autoscaler code](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/pkg/controller/podautoscaler/autoscaler.go)
- [Foretoken autoscaling architecture](../../../../control-plane/internal/autoscaling/README.md)
- [Foretoken public policies](../../../../docs/autoscaling.md)
- [model loading and storage](../model-loading-storage/)
