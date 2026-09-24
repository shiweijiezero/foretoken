<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

`foretoken-frontend` serves OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages at the same address. Declare a `FrontendService` through a maintained example or your own service configuration; Foretoken creates and configures the frontend workloads automatically.

## Use it

Follow the repository [Quick Start](../../README.md) to deploy a frontend and make a request. One frontend can serve multiple public models. The request `model` selects the model; requests are never silently redirected to another model when that model is unavailable.

The frontend supports collected JSON and SSE streaming responses, completions and chat completions, tokenization, tools, reasoning, structured output, and capability-gated image input. Image input currently accepts bounded base64 `data:` content, not remote media URLs.

Configure aggregate serving or separate prefill/decode (P/D) or encoder/prefill/decode (E/P/D) stages in `ModelService`. Disaggregated serving requires platform support for the selected runtime and transport.

## Inference APIs

Use the frontend address returned by `foretoken endpoint` for all three protocols. The request path selects the protocol; no separate service or protocol setting is needed.

| API | POST path | Conversation input |
| --- | --- | --- |
| OpenAI Chat Completions | `/v1/chat/completions` | `messages` |
| OpenAI Responses | `/v1/responses` | `input`; use `store: false` and send the conversation history on each turn |
| Anthropic Messages | `/v1/messages` | `messages` and the required `max_tokens` output budget |
| Anthropic token counting | `/v1/messages/count_tokens` | `messages`, with the same system prompt and tools as generation |

All generation endpoints accept `stream: true` for SSE. Clients execute tools and return their results in the next request. Responses accepts function tools, namespaced functions, and unconstrained custom-text tools; server-hosted tools such as web search require a separate execution service and are not accepted here. Responses does not retain conversation history or provide background execution.

Forced tool choice and strict tool schemas require a model service with structured-output support. Thinking controls depend on the model's chat template. Output budgets include reasoning tokens even when the response hides reasoning; Messages accepts a total `max_tokens` budget, not a separate `thinking.budget_tokens` allowance.

## Endpoint access

The default mode exposes the frontend through a `LoadBalancer` Service. Gateway mode uses an `HTTPRoute` attached to a platform Gateway and exposes `/v1`, `/tokenize`, and `/detokenize`. Configure DNS, TLS, authentication, and other ingress policies for your Gateway deployment. Operator endpoint access in the default mode depends on the LoadBalancer and cluster network policy.

| Access | Endpoints | Purpose |
| --- | --- | --- |
| Client | `/v1/*`, `/tokenize`, `/detokenize` | Submit inference and discover configured models |
| Operator | `/healthz`, `/readyz`, `/statusz`, `/metrics` | Probes, runtime diagnostics, and Prometheus scraping |

`/v1/models` lists models in the frontend's active serving configuration.

`/healthz` reports that the frontend process is running. `/readyz` reports that a serving configuration is active and the frontend can accept new requests. It does not prove every configured model has a healthy backend path. `/statusz` reports runtime and KV-index diagnostics for platform operators. `/metrics` is the Prometheus scrape endpoint.

Model configuration updates are loaded within the running frontend once prepared; requests already executing retain their selected configuration. Changes to `FrontendService.spec.routerPipeline` roll out through the frontend Deployment.
