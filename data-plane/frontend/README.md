<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

English | [简体中文](README_zh.md)

The frontend provides text and video generation APIs for deployed models.

## Generate text

Follow the repository [Quick Start](../../README.md#quick-start) to deploy a service, then send a request from the repository root:

```bash
DEPLOYMENT=examples/quickstart
FRONTEND_URL="$(foretoken endpoint "$DEPLOYMENT")"
REQUEST_HOST="$(foretoken endpoint "$DEPLOYMENT" --host)"

# OpenAI Responses
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","input":"Hello","max_output_tokens":512,"store":false}'

# Anthropic Messages
curl --fail-with-body "$FRONTEND_URL/v1/messages" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"max_tokens":512}'
```

Add `"stream": true` and `curl --no-buffer` to receive output as it is generated.

| API | Path | Request input |
| --- | --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` | `model`, `messages` |
| OpenAI Responses | `POST /v1/responses` | `model`, `input`; set `store: false` and send the conversation history on each turn |
| Anthropic Messages | `POST /v1/messages` | `model`, `messages`, and a required `max_tokens` budget |
| Anthropic token counting | `POST /v1/messages/count_tokens` | The same model, messages, system prompt, and tools used for generation |
| Text completions | `POST /v1/completions` | `model`, `prompt` |

`GET /v1/models` lists configured model identifiers. `/tokenize` and `/detokenize` convert between text and token IDs. Image-capable text models accept base64 image `data:` URLs.

Tools run in the client, which sends their results in the next request. Responses supports function tools, namespaced functions, and custom-text tools, but not server-hosted tools or background execution. Forced tool choice and strict tool schemas require structured-output support in the model.

Some tool parsers use a structural-tag grammar to constrain forced or strict tool calls. If the model's parser and grammar backend support it, add `structuralTag` to the ModelService's existing structured-output formats:

```yaml
spec:
  features:
    structuredOutputs: [structuralTag]
```

For configurations with `spec.modelPools`, declare this capability in each applicable pool's `features.structuredOutputs` instead of the top-level `features`.

Output budgets include reasoning tokens. Messages uses `max_tokens`, not a separate `thinking.budget_tokens`; thinking controls depend on the model's chat template. When the budget is exhausted, Messages reports `max_tokens` and Responses reports `incomplete`. Execute only complete tool calls.

## Generate video

`POST /v1/videos/sync` accepts a prompt and reference media as multipart form data and returns the generated video. The [MiniMax H3 recipe](../../examples/recipes/minimax-h3/a100-bf16-tp2/README.md#generate-from-an-image) provides deployment and request commands that save the result to `./data/video.mp4`.

### Background generation

To submit a request and retrieve the result later, enable tasks in the deployment's `frontend.yaml`:

```yaml
spec:
  videoTasks:
    claimName: video-results
    retentionSeconds: 86400
```

`video-results` must be an existing dedicated persistent volume claim (PVC) in the same namespace. Use ReadWriteMany storage across nodes. Place the reference image at `inputs/reference.png` in this volume, then apply the configuration:

```bash
DEPLOYMENT=examples/recipes/minimax-h3/a100-bf16-tp2
foretoken deploy "$DEPLOYMENT" --timeout 1h
FRONTEND_URL="$(foretoken endpoint "$DEPLOYMENT")"
REQUEST_HOST="$(foretoken endpoint "$DEPLOYMENT" --host)"
mkdir -p ./data
```

This request selects the ModelService named `h3`. Input paths are relative to the video storage volume:

```bash
curl --fail-with-body "$FRONTEND_URL/v1/videos" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{
    "modelServiceRef": {"name": "h3"},
    "request": {
      "task": "fl2va",
      "prompt": "A sailboat crossing a calm bay at sunrise",
      "width": 1024, "height": 576,
      "numFrames": 124, "fps": 24, "numInferenceSteps": 50,
      "inputFiles": [{
        "field": "input_reference",
        "path": "inputs/reference.png",
        "contentType": "image/png"
      }]
    }
  }'
```

The response contains a task `id`, `status_url`, and `content_url`. Use the same frontend address and replace `{id}` below with the returned task ID:

| Action | Endpoint | Usage |
| --- | --- | --- |
| Check status | `GET /v1/videos/{id}` | Wait for `phase` to become `Succeeded`; for `Failed`, read `reason` and `message` |
| Save the video | `GET /v1/videos/{id}/content` | After successful generation, save the response with `curl --output ./data/video.mp4` |
| Cancel | `POST /v1/videos/{id}/cancel` | Request cancellation; backend computation may still be finishing |
| Delete | `DELETE /v1/videos/{id}` | Remove the task and its files |

Cancellation and deletion continue after the HTTP `202` response. The configuration above retains results for one day after a task ends, then removes them automatically. Original reference files are retained.

## Configure admission rules

Admission controls concurrency and queuing for text generation and tokenization. The default is unrestricted (`allow_all`). Set `FrontendService.spec.admission` to provide defaults for every model, for example:

```yaml
spec:
  admission:
    algorithm: concurrency
    parameters:
      maxConcurrentRequests: 64
```

Limits apply independently per frontend replica, per model; there is no shared frontend-wide limit or queue. Choose the limit for your workload; batches count each output candidate separately. To allow queuing, add `maxQueuedRequests` and optionally `queueTimeout` under `parameters`.

A model's `ModelService.spec.admission` replaces the entire frontend default, rather than merging parameters. For example, to leave one model unrestricted:

```yaml
spec:
  admission:
    algorithm: allow_all
```

Redeploy the service configuration to apply changes. Admission updates and model additions or removals do not restart frontend Pods. New requests may receive HTTP 503 while that model's rule is being replaced.

See [Observability](../../observability/README.md) to inspect admission results, or [Implementing admission rules](../../docs/development/admission-rules.md) to add an algorithm.


## Operations

Use `foretoken status` to inspect a deployment and `foretoken delete` to remove it, passing its configuration directory to either command.

| Endpoint | Purpose |
| --- | --- |
| `/healthz` | Process liveness |
| `/readyz` | HTTP readiness after a valid routing configuration is loaded |
| `/statusz` | Serving and cache-index status |
| `/metrics` | Prometheus metrics |

The HTTP frontend stays reachable while models start or change. A valid configuration with no models is also HTTP-ready; inference requests then return HTTP 503. Check `serving_ready` in `/statusz` for serving readiness.

For gateway configuration, see [Gateway mode](../../README.md#gateway-mode). Configure TLS and authentication at the cluster ingress; network policies govern access to operator endpoints.
