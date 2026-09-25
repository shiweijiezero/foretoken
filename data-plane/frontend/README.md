<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

The frontend serves OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages at one address. Requests select a configured model through the `model` field.

## Send a request

Deploy the repository [Quick Start](../../README.md#quick-start), which includes a Chat Completions example. From the repository root, use the same deployment for Responses or Messages:

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

# OpenAI Responses
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","input":"Hello","max_output_tokens":512,"store":false}'

# Anthropic Messages
curl --fail-with-body "$FRONTEND_URL/v1/messages" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"max_tokens":512}'
```

These requests return JSON. Add `"stream": true` to receive incremental server-sent events (SSE), and use `curl --no-buffer` to display them as they arrive.

## Choose an API

| API | POST path | Conversation input |
| --- | --- | --- |
| OpenAI Chat Completions | `/v1/chat/completions` | `messages` |
| OpenAI Responses | `/v1/responses` | `input`; set `store: false` and send the conversation history on each turn |
| Anthropic Messages | `/v1/messages` | `messages` and a required `max_tokens` output budget |
| Anthropic token counting | `/v1/messages/count_tokens` | `messages`, with the same system prompt and tools as generation |

`GET /v1/models` lists the configured model identifiers. Text completions use `POST /v1/completions`; `/tokenize` and `/detokenize` convert between text and token IDs.

Tools run in the client, which sends their results in the next request. Responses supports function tools, namespaced functions, and unconstrained custom-text tools. Server-hosted tools and background responses are unsupported.

Forced tool choice and strict tool schemas require structured-output support in the model service. Thinking controls depend on the model's chat template. Output budgets include reasoning tokens; Messages uses `max_tokens` for the total budget and does not accept a separate `thinking.budget_tokens` allowance.

When the output budget is exhausted, Messages reports `max_tokens` and Responses reports `incomplete`. Execute only complete tool calls; interrupted calls may be omitted or contain partial arguments.

Image-capable model services accept base64 image `data:` URLs rather than remote image URLs.

## Access and operations

The default endpoint uses a Kubernetes LoadBalancer. For hostname-based access, see [Gateway mode](../../README.md#gateway-mode). Configure TLS and authentication at the cluster's ingress.

| Endpoint | Purpose |
| --- | --- |
| `/healthz` | Frontend process liveness |
| `/readyz` | Frontend readiness to accept requests |
| `/statusz` | Serving and cache-index diagnostics |
| `/metrics` | Prometheus metrics |

Access to operator endpoints follows the cluster's network policy; Gateway mode exposes the client paths `/v1`, `/tokenize`, and `/detokenize`.

After changing a service configuration, reapply it with `foretoken deploy`. Use `foretoken status` to inspect it and `foretoken delete` to remove it, passing the same configuration directory to each command.
