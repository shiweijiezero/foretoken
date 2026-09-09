<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Grafana dashboard design references

`foretoken-system-overview.json` is the single operator dashboard shipped by Foretoken. Its information architecture follows the [Dynamo Dashboard](https://github.com/ai-dynamo/dynamo/blob/main/deploy/observability/grafana-dynamo-dashboard-configmap.yaml): headline service health, Frontend request flow, model-serving performance, cache behavior, and resource usage appear in that order.

The vLLM latency, throughput, scheduler, and cache panels were cross-checked against the [AIBrix Engine Dashboard (vLLM)](https://github.com/vllm-project/aibrix/blob/140e5238213adc25c672df2d0272fbc4c00199d2/observability/grafana/AIBrix_vLLM_Engine_Dashboard.json). Queries consume Foretoken recording rules rather than copying another framework's metric namespace, so the dashboard and Prometheus contract remain owned together.
