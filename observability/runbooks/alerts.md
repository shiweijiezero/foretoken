<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken alert runbooks

These alerts are initial warning signals. They do not prove a user-visible
outage and do not trigger remediation. Check the signal boundary and the
current Kubernetes state before taking action.

Start with the alert labels, then inspect the corresponding namespace and
workload:

```bash
kubectl get pods,services,endpointslices -n <namespace> -o wide
```

If a model-server recording series disappears, first check raw `up` and Pod
health. Missing metrics mean unavailable data, not a value of zero.

## ForetokenMetricsTargetDown

Prometheus has continuously failed to scrape an already discovered Frontend or
model-server target for five minutes.

1. Open the Prometheus Targets page and inspect the target's `lastError`.
2. Check whether the selected Pod is running and whether its `/metrics`
   listener responds from an allowed monitoring Pod.
3. Check the Service endpoint, named port, ServiceMonitor selector, and
   NetworkPolicy.
4. If the application is unhealthy, inspect its logs and recent rollout events.

This alert checks scrape reachability only. It does not detect a target that
vanishes completely from service discovery, and `up == 0` is not equivalent to
Foretoken request readiness or a confirmed user outage.

## ForetokenFrontendHTTPResponseStart5xxRatioHigh

More than 5% of Frontend HTTP response starts have been 5xx for ten minutes,
while traffic has remained at or above 0.1 response starts per second.

1. In the Overview dashboard, filter to the alert's namespace and Frontend
   service, then split response starts by handler and status.
2. Inspect Frontend logs and recent configuration or routing changes.
3. Check backend availability if the affected handler performs inference.

The metric records the status when the HTTP response object starts. A stream
that fails later may still have started with 2xx, so this is not an inference
failure ratio or SLA.

## ForetokenModelServerSchedulerBacklog

The aggregated vLLM stage scheduler queue has remained nonzero for ten minutes.

1. Inspect running and waiting requests for the alert's model group and role.
2. Check KV-cache pressure, Pod health, accelerator utilization, and recent
   traffic changes.
3. Compare the affected P/D role separately; do not sum prefill and decode into
   a user-level request count.

This is a vLLM stage queue, not the number of users and not the Frontend
admission queue. A transient nonzero queue is normal; the alert requires a
continuous ten-minute backlog.

## ForetokenModelServerKVCachePressureHigh

The highest KV-cache usage among engines in a model group has remained at or
above 95% for ten minutes.

1. Confirm the group, role, and model labels, then inspect scheduler waiting and
   running requests.
2. Check workload configuration, request lengths, concurrency, and replica
   health before changing capacity.
3. Compare individual Pods or engines to find the hotspot.

The recorded value is the maximum across engines at each evaluation, not a
fleet average; the engine contributing the maximum can change over time. The
95% threshold is an initial warning policy and should be tuned using production
baselines.
