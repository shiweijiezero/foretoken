<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

`foretoken-frontend` receives inference traffic and returns OpenAI-compatible responses. A `FrontendService` is created and configured by the Foretoken Controller; users deploy it through a maintained example or their own service configuration rather than starting it directly.

The frontend is exposed directly through a `LoadBalancer` Service in the default mode, or through an `HTTPRoute` attached to a platform Gateway in Gateway mode. The Gateway owns DNS, TLS, authentication, and other ingress policy. Gateway mode exposes `/v1`, `/tokenize`, and `/detokenize` through the route, not the operator endpoints. In the default mode, operator endpoint reachability depends on the LoadBalancer and cluster network policy.

## Use it

Follow the repository [Quick Start](../../README.md) to deploy a frontend and make a request. One frontend can serve multiple public models. The request `model` selects the model; requests are never silently redirected to another model when that model is unavailable.

The frontend supports collected JSON and SSE streaming responses, completions and chat completions, tokenization, tools, reasoning, structured output, and capability-gated image input. Image input currently accepts bounded base64 `data:` content, not remote media URLs.

The Controller configures and validates aggregate, Prefill/Decode, and Encoder/Prefill/Decode topologies. The frontend routes each stage within that controller-owned topology. Current disaggregated serving requires the runtime profiles and transports accepted by the Controller; it is not configured through frontend request parameters.

## Endpoint access

| Access | Endpoints | Purpose |
| --- | --- | --- |
| Client | `/v1/*`, `/tokenize`, `/detokenize` | Submit inference and discover configured models |
| Operator | `/healthz`, `/readyz`, `/statusz`, `/metrics` | Probes, runtime diagnostics, and Prometheus scraping |
| Controller internal | `/internal/autoscaling/telemetry` | Controller telemetry collection; not a client contract |

`/v1/models` lists models in the current published runtime generation. Listing a model does not guarantee its backends remain healthy at the instant of a later request; clients must handle an unavailable response.

`/healthz` reports that the frontend process is running. `/readyz` reports that a published runtime generation is available to accept new requests. It does not prove every configured model has a healthy backend path. `/statusz` reports runtime and KV-index diagnostics for platform operators. `/metrics` is the Prometheus scrape endpoint.

When models or routes change, the frontend activates a new runtime generation only after it is published successfully. Invalid or unready updates do not replace the active generation, and accepted requests continue on their generation.
