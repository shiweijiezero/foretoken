<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

`foretoken-frontend` receives inference traffic and returns OpenAI-compatible responses. Declare a `FrontendService` through a maintained example or your own service configuration; Foretoken creates and configures the frontend workloads automatically.

## Use it

Follow the repository [Quick Start](../../README.md) to deploy a frontend and make a request. One frontend can serve multiple public models. The request `model` selects the model; requests are never silently redirected to another model when that model is unavailable.

The frontend supports collected JSON and SSE streaming responses, completions and chat completions, tokenization, tools, reasoning, structured output, and capability-gated image input. Image input currently accepts bounded base64 `data:` content, not remote media URLs.

Configure aggregate serving or separate prefill/decode (P/D) or encoder/prefill/decode (E/P/D) stages in `ModelService`. Disaggregated serving requires platform support for the selected runtime and transport.

## Endpoint access

The default mode exposes the frontend through a `LoadBalancer` Service. Gateway mode uses an `HTTPRoute` attached to a platform Gateway and exposes `/v1`, `/tokenize`, and `/detokenize`. Configure DNS, TLS, authentication, and other ingress policies for your Gateway deployment. Operator endpoint access in the default mode depends on the LoadBalancer and cluster network policy.

| Access | Endpoints | Purpose |
| --- | --- | --- |
| Client | `/v1/*`, `/tokenize`, `/detokenize` | Submit inference and discover configured models |
| Operator | `/healthz`, `/readyz`, `/statusz`, `/metrics` | Probes, runtime diagnostics, and Prometheus scraping |

`/v1/models` lists models in the frontend's active serving configuration.

`/healthz` reports that the frontend process is running. `/readyz` reports that a serving configuration is active and the frontend can accept new requests. It does not prove every configured model has a healthy backend path. `/statusz` reports runtime and KV-index diagnostics for platform operators. `/metrics` is the Prometheus scrape endpoint.

Model configuration updates are loaded within the running frontend once prepared; requests already executing retain their selected configuration. Changes to `FrontendService.spec.routerPipeline` roll out through the frontend Deployment.
