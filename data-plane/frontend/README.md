<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

`foretoken-frontend` receives inference traffic and returns OpenAI-compatible responses. Declare a `FrontendService` through a maintained example or your own service configuration; Foretoken creates and configures the frontend workloads automatically.

The frontend is exposed directly through a `LoadBalancer` Service in the default mode, or through an `HTTPRoute` attached to a platform Gateway in Gateway mode. The Gateway owns DNS, TLS, authentication, and other ingress policy. Gateway mode exposes `/v1`, `/tokenize`, and `/detokenize` through the route, not the operator endpoints. In the default mode, operator endpoint reachability depends on the LoadBalancer and cluster network policy.

## Use it

Follow the repository [Quick Start](../../README.md) to deploy a frontend and make a request. One frontend can serve multiple public models. The request `model` selects the model; requests are never silently redirected to another model when that model is unavailable.

The frontend supports collected JSON and SSE streaming responses, completions and chat completions, tokenization, tools, reasoning, structured output, and capability-gated image input. Image input currently accepts bounded base64 `data:` content, not remote media URLs.

Configure aggregate serving or separate prefill/decode (P/D) or encoder/prefill/decode (E/P/D) stages in `ModelService`. Disaggregated serving requires platform support for the selected runtime and transport; frontend request parameters do not change the deployment topology.

## Endpoint access

| Access | Endpoints | Purpose |
| --- | --- | --- |
| Client | `/v1/*`, `/tokenize`, `/detokenize` | Submit inference and discover configured models |
| Operator | `/healthz`, `/readyz`, `/statusz`, `/metrics` | Probes, runtime diagnostics, and Prometheus scraping |
| Controller internal | `/internal/autoscaling/telemetry` | Controller telemetry collection; not a client contract |

`/v1/models` lists models in the frontend's active serving configuration. Listing a model does not guarantee its backends remain healthy at the instant of a later request; clients must handle an unavailable response.

`/healthz` reports that the frontend process is running. `/readyz` reports that a serving configuration is active and the frontend can accept new requests. It does not prove every configured model has a healthy backend path. `/statusz` reports runtime and KV-index diagnostics for platform operators. `/metrics` is the Prometheus scrape endpoint.

Model and routing updates take effect only after successful preparation. Invalid or unready updates leave the active configuration unchanged, and requests already executing continue using the configuration they started with.
