<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Lark alert notifications

English | [简体中文](README_zh.md)

This optional integration routes alerts labeled `service=foretoken` from an existing Prometheus Alertmanager to a Lark custom bot. Foretoken owns the alert labels and message fields; the platform owns the Alertmanager instance, the destination, and the webhook credential.

The manifest is a deployment source, not a second Alertmanager. Applying it creates an `AlertmanagerConfig` in the cluster. Keep the webhook URL in a Kubernetes Secret and never add it to this repository.

Foretoken adds a `notification_language` label to each alert. Set
`observability.alerts.language` to `zh`, `en`, or `bilingual` in the
[observability example](../../../examples/observability/observability.yaml)
and pass it to `foretoken install --values`. The payload uses that label to
render Chinese, English, or both languages. Because one Alertmanager message
can contain several alerts, the choice applies to the deployment's shared
messages rather than to individual recipients.

## Prerequisites

- Prometheus Operator selects `AlertmanagerConfig` resources for the target Alertmanager;
- the installed CRD and Alertmanager version support `webhookConfigs.payload`;
- a Lark custom bot webhook is available;
- the platform's namespace-matching policy permits alerts from the Foretoken workload namespaces.

Prometheus Operator normally scopes an `AlertmanagerConfig` route to its namespace. A configuration stored beside Alertmanager in `monitoring` may therefore reject alerts carrying a different workload `namespace` label. The Alertmanager owner must inspect `alertmanagerConfigMatcherStrategy` and choose the cross-namespace policy deliberately; this integration does not change it.

## Install

Choose the namespace that owns Alertmanager and create the referenced Secret there:

```bash
ALERTMANAGER_NAMESPACE=monitoring
kubectl create secret generic foretoken-lark-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --from-literal=url='<LARK_CUSTOM_BOT_WEBHOOK_URL>'
```

Apply the receiver in the same namespace:

```bash
kubectl apply \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --filename observability/integrations/lark/alertmanagerconfig.yaml
```

If the Secret already exists, update it through the platform's secret-management workflow rather than committing its value.

## Message contract

The route selects `service=foretoken`, the common label installed by Foretoken's alert rules. The payload groups alerts by the stable machine alert name and translates the display name (`alertname_zh`), severity, field captions, summary, and description. Resource names and device IDs remain unchanged. Each target includes firing and resolved timestamps in `Asia/Shanghai`.

GPU messages include the evaluated reading, unit, threshold, and persistence window. Resolved notifications retain the last evaluated annotations rather than taking a new device measurement.

Both normalized recording-rule labels such as `model_group` and raw ServiceMonitor labels such as `inference_foretoken_io_model_group` are handled because scrape failures occur before recording-rule normalization.

## Verify

Inspect the accepted resource and the Alertmanager routing tree:

```bash
kubectl get alertmanagerconfig foretoken-lark \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --output yaml
```

Test with an alert carrying the same labels as production, including `service=foretoken` and a real Foretoken workload namespace. A test in only the Alertmanager namespace does not verify cross-namespace routing. Confirm both firing and resolved messages in Lark.

## Design references

Alertmanager remains responsible for grouping, deduplication, repetition, and delivery. The payload iterates over `.Alerts` instead of assuming that every grouped alert has identical annotations.

- [Prometheus notification template reference](https://prometheus.io/docs/alerting/latest/notifications/)
- [Prometheus Operator `AlertmanagerConfig` API](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1alpha1.AlertmanagerConfig)
- [vLLM Production Stack observability configuration](https://github.com/vllm-project/production-stack/blob/main/observability/kube-prom-stack.yaml)
- [Dynamo XPU alert rules](https://github.com/ai-dynamo/dynamo/blob/main/dev/observability/xpu-alert-rules.yml)

Dynamo's XPU rules provide useful examples of persistence windows, warning and critical levels, exporter availability, and traffic-gated hardware symptoms. vLLM's Production Stack provides a receiver template that iterates over grouped alerts. Neither upstream defines Foretoken's Lark destination, labels, or operator response, so those remain explicit Foretoken integration policy.
