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

Admission bounds waiting work and caller concurrency for text generation. Add these fields under `spec` in the deployment's `frontend.yaml`:

```yaml
spec:
  admission:
    maxWaitingRequests: 128
    queueTimeout: 30s
  roleRules:
    - role: role1
      priority: 10
      perCaller:
        maxWaitingRequests: 8
        maxConcurrentRequests: 4
    - role: role2
      priority: 0
      perCaller:
        maxWaitingRequests: 32
        maxConcurrentRequests: 8
```

For each model, limits are shared across all replicas of the FrontendService; each caller has a separate allowance. Role rules require a waiting-capacity limit.

| Setting | Meaning |
| --- | --- |
| `admission.maxWaitingRequests` | Total waiting capacity, including input preparation |
| `admission.queueTimeout` | Waiting and preparation time allowed before backend acceptance |
| `perCaller.maxWaitingRequests` | Waiting capacity for one caller |
| `perCaller.maxConcurrentRequests` | Dispatched work for one caller, counted until execution ends |

Batched prompts and `best_of` count each generated sequence. Higher `priority` values dispatch first among eligible requests. At equal priority, each frontend rotates between callers and preserves their request order. A caller at its concurrency limit does not block others. To prioritize the vLLM queue as well, set `scheduling-policy: priority` in the model's `engineArgs`.

The trusted gateway overwrites these headers with the authenticated caller's role and a stable identifier, such as a key identifier:

```http
x-role: role1
x-caller-id: caller-a
```

All text APIs use these headers. With role rules enabled, requests without a caller identifier or matching role are rejected. Configure authentication, authorization, and RPM/token quotas at the gateway, and restrict frontend access to that trusted gateway. Tokenization and detokenization use waiting capacity only; these rules do not apply to video APIs.

`ModelService.spec.admission` and `ModelService.spec.roleRules` independently replace the corresponding frontend defaults as whole blocks. To leave frontend admission unrestricted for one model, set both `admission: {}` and `roleRules: []`. Omitting both settings at the frontend leaves admission unrestricted by default.

A model's `roleRules[].allowedPools` restricts a role to names in `spec.modelPools`. A disaggregated model must retain a Pool for every required execution stage.

To limit how many unfinished requests each text model-server instance accepts, add `instanceAdmission` to the model YAML. This includes requests waiting in the engine queue. Omit it for unlimited acceptance:

```yaml
spec:
  instanceAdmission:
    maxConcurrentRequests: 32
```

After upgrading the platform, run `foretoken deploy` once to select the current model application. Later limit changes apply online. Existing work continues; when the limit is full, new requests receive `503` until occupancy falls below it.

Apply changes using [Update serving settings](#update-serving-settings). Before a response starts, a full queue or expired admission wait returns 503, the total request timeout returns 504, and a batch exceeding capacity returns 400. Enabling limits can reject requests already waiting. Cancelled requests count toward concurrency until execution ends.

When upgrading from `admission.algorithm` and `admission.parameters`, replace those fields with the settings above and redeploy the frontend and models together. Old fields are rejected; platform installation alone does not replace running applications.

See [Observability](../../observability/README.md) for queue and dispatch results. Maintainers extending request processing or backend integration can refer to [Admission lifecycle](../../docs/development/admission-rules.md).

## Update serving settings

Edit the deployment YAML and run `foretoken deploy` again. For example, change logging and request budgets under the existing `spec` in `examples/quickstart/frontend.yaml`:

```yaml
spec:
  logLevel: debug
  timeouts:
    request: 15m
    streamIdle: 5m
```

Apply the Quick Start configuration:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

The command waits for changes to take effect. [Routing strategies](src/router/README.md), admission rules, logging, and request timeouts update online; invalid settings leave the working configuration unchanged.

| Setting | Purpose |
| --- | --- |
| `timeouts.request` | Total request duration |
| `timeouts.streamIdle` | Maximum gap between response chunks; must not exceed `request` |
| `timeouts.drain` | Shutdown wait for accepted requests, default `10m`; changing it replaces frontend Pods |
| `logLevel` | `trace`, `debug`, `info` (default), `warn`, `error`, or `off` |

Timeout changes apply to new requests. To limit prompt length, set `ModelService.spec.maxInputTokens`, or the corresponding `spec.modelPools[].maxInputTokens`, and redeploy. This setting updates online and is separate from the engine's combined input/output context limit.

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
