<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

[简体中文](README_zh.md)

`foretoken-frontend` is the Rust data plane behind the gateway that directly handles inference requests. It provides a consistent generation API and manages request routing, streaming responses, and runtime availability across multiple models and inference instances.

## Capabilities

- OpenAI-compatible Completion and Chat Completion APIs;
- multiple public models through one frontend;
- instance selection based on model compatibility, request capabilities, health, load, and KV cache reuse opportunities;
- aggregated, Prefill/Decode, and Encoder/Prefill/Decode serving topologies;
- both SSE streaming and collected JSON responses;
- tools, reasoning, structured output, and capability-gated image input;
- uninterrupted accepted requests while routes and instances change;
- health, readiness, and runtime metrics endpoints.

## Request path

```text
Client
  ↓
LoadBalancer or Gateway
  ↓
foretoken-frontend
  ├─ validates and prepares the request
  ├─ selects the model and an available inference instance
  ├─ executes an aggregated or disaggregated generation flow
  └─ returns a streaming or collected response
  ↓
Model service
```

The request's `model` field selects the public model. The frontend sends it only to instances that support that model and the requested capabilities. If one model is temporarily unavailable, other healthy models continue serving; requests are never silently redirected to a different model.

When model instances or routes change, the frontend activates the new configuration only after it is ready. Invalid or unready updates do not replace the active configuration or interrupt requests already in progress.

## Access

### Local mode

Local mode obtains an address from a `LoadBalancer` Service. See the repository Quick Start for the access command.

### Gateway mode

After a Gateway Controller is installed, the Foretoken Chart can create a dedicated `GatewayClass` and `Gateway` or reuse an existing platform Gateway. Foretoken creates the frontend's `HTTPRoute`, while the platform Gateway continues to own DNS, TLS, authentication, and other ingress policies.

## HTTP APIs

- `POST /v1/completions`
- `POST /v1/chat/completions`
- `POST /v1/generate`
- `POST /tokenize`
- `POST /detokenize`
- `GET /v1/models`
- `GET /v1/models/{model}`
- `GET /healthz`
- `GET /readyz`
- `GET /metrics`

Streaming and non-streaming requests have the same generation semantics. Image input currently accepts bounded base64 `data:` content; remote media URLs are not accepted.

## Health and readiness

- `/healthz` reports whether the frontend process is operating;
- `/readyz` reports whether at least one model route can accept inference requests;
- `/v1/models` lists only models with a currently healthy inference path.

The Foretoken Controller normally creates and configures the frontend; users do not need to start or maintain it separately.
