<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

English | [简体中文](README_zh.md)

The frontend provides text and video generation APIs for deployed models.

## Generate text

Follow the repository [Quick Start](../../README.md#quick-start) to deploy a service, then send a request from the repository root. If your gateway uses API keys, set `API_KEY` to the key it issued and add `-H "Authorization: Bearer $API_KEY"` to every request below.

```bash
DEPLOYMENT=examples/quickstart/qwen3-0.6b
FRONTEND_URL="$(foretoken endpoint "$DEPLOYMENT")"
REQUEST_HOST="$(foretoken endpoint "$DEPLOYMENT" --host)"

# OpenAI Responses
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","input":"Hello","max_output_tokens":512}'

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
| OpenAI Responses | `POST /v1/responses` | `model`, `input` |
| Anthropic Messages | `POST /v1/messages` | `model`, `messages`, and a required `max_tokens` budget |
| Anthropic token counting | `POST /v1/messages/count_tokens` | The same model, messages, system prompt, and tools used for generation |
| Text completions | `POST /v1/completions` | `model`, `prompt` |

`GET /v1/models` lists configured model identifiers. `/tokenize` and `/detokenize` convert between text and token IDs. Image-capable text models accept base64 image `data:` URLs.

Tools run in the client, which sends their results in the next request. Responses supports function tools, namespaced functions, and custom-text tools, but not server-hosted tools. Forced tool choice and strict tool schemas require structured-output support in the model.

Some tool parsers use a structural-tag grammar to constrain forced or strict tool calls. If the model's parser and grammar backend support it, add `structuralTag` to the ModelService's existing structured-output formats:

```yaml
spec:
  features:
    structuredOutputs: [structuralTag]
```

For configurations with `spec.modelPools`, declare this capability in each applicable pool's `features.structuredOutputs` instead of the top-level `features`.

Output budgets include reasoning tokens. Messages uses `max_tokens`, not a separate `thinking.budget_tokens`; thinking controls depend on the model's chat template. When the budget is exhausted, Messages reports `max_tokens`; Responses reports `status: "incomplete"` with `incomplete_details.reason: "max_output_tokens"`. Responses JSON, terminal SSE events, and stored retrieval have the same final status, output items, and usage. Execute only complete tool calls.

### Continue a conversation

Responses defaults to `store: true`, using a shared in-memory cache across a FrontendService's replicas. To continue the first request above without resending its history, replace `resp_...` with the returned `id`:

```bash
RESPONSE_ID=resp_...
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"Qwen/Qwen3-0.6B\",\"previous_response_id\":\"$RESPONSE_ID\",\"input\":\"What did I just say?\",\"max_output_tokens\":512}"
```

Resend any `instructions` or `tools` needed for the new turn. To reuse an individual stored item instead, pass `[{"type":"item_reference","id":"..."}]` as `input`; use an item ID from `output[].id` or the input listing's `data[].id`.

| Action | Endpoint |
| --- | --- |
| Retrieve status and output | `GET /v1/responses/{id}` |
| List the expanded input history | `GET /v1/responses/{id}/input_items` |
| Delete a saved response | `DELETE /v1/responses/{id}` |

To keep multiple turns in one conversation, create it once:

```bash
curl --fail-with-body "$FRONTEND_URL/v1/conversations" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Replace `conv_...` with this response's `id`, then use `conversation` instead of `previous_response_id`:

```bash
CONVERSATION_ID=conv_...
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"Qwen/Qwen3-0.6B\",\"conversation\":\"$CONVERSATION_ID\",\"input\":\"Hello\",\"max_output_tokens\":512,\"store\":false}"
```

Each turn loads the conversation history and appends its new input and output. Here `store: false` opts out of saving the Response object, not the conversation history. Wait for the active response to finish before starting another turn or changing items. For fully client-managed history, use `store: false` without `conversation` or `previous_response_id`, and include the required prior input and output items in each request.

| Action | Endpoint |
| --- | --- |
| Retrieve a conversation or replace metadata with `{"metadata":{...}}` | `GET` or `POST /v1/conversations/{id}` |
| Delete the container while retaining its items | `DELETE /v1/conversations/{id}` |
| List items or append `{"items":[...]}` | `GET` or `POST /v1/conversations/{id}/items` |
| Retrieve an item or remove it from the conversation | `GET` or `DELETE /v1/conversations/{id}/items/{item_id}` |

Item listings support `limit`, `order`, and `after` or `before` item-ID cursors.

### Run Responses in the background

Set `background: true` to return a response ID while generation continues independently of the HTTP connection:

```bash
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","input":"Explain how a rainbow forms.","max_output_tokens":512,"background":true}'
```

Set `RESPONSE_ID` to this request's returned `id` and poll:

```bash
curl --fail-with-body "$FRONTEND_URL/v1/responses/$RESPONSE_ID" \
  -H "Host: $REQUEST_HOST"
```

Keep polling while `status` is `queued` or `in_progress`. Terminal states are `completed`, `incomplete`, `failed`, and `cancelled`. To request cancellation, send `POST /v1/responses/{id}/cancel`, then poll until execution ends. Deleting a response does not cancel it. Background work still uses the service's admission rules and request timeout.

For resumable SSE, create the request with both `background: true` and `stream: true`, using `curl --no-buffer`. Set `RESPONSE_ID` from `response.id` in the `response.created` event and replace `42` below with the last event `sequence_number` your client processed:

```bash
SEQUENCE_NUMBER=42
curl --fail-with-body --no-buffer \
  "$FRONTEND_URL/v1/responses/$RESPONSE_ID?stream=true&starting_after=$SEQUENCE_NUMBER" \
  -H "Host: $REQUEST_HOST"
```

This delivers events after that sequence and follows any remaining generation; it does not rerun the request. Stream recovery requires both flags on the original request and is limited to the [event retention window](#operations).

### Save Chat Completions

Chat Completions defaults to `store: false`. Set `store: true` to archive the final completion and its original input messages, for either JSON or streaming requests:

```bash
curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"max_tokens":512,"store":true}'
```

Use the returned completion `id` for `{id}` below:

| Action | Endpoint |
| --- | --- |
| List archives; optionally filter by `model` or `metadata[key]` | `GET /v1/chat/completions` |
| Retrieve the final completion | `GET /v1/chat/completions/{id}` |
| List original input messages | `GET /v1/chat/completions/{id}/messages` |
| Replace metadata with a `{"metadata":{...}}` body | `POST /v1/chat/completions/{id}` |
| Delete an archive | `DELETE /v1/chat/completions/{id}` |

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

To limit waiting text requests, add `admission` under `spec` in the deployment's `frontend.yaml`:

```yaml
spec:
  admission:
    maxWaitingRequests: 128
    queueTimeout: 30s
```

The waiting limit is shared across all FrontendService replicas, separately for each model, and includes input preparation. `queueTimeout` bounds waiting and preparation before backend acceptance. Batched prompts and `best_of` count each generated sequence. These rules do not apply to video APIs.

Apply the YAML through [Update serving settings](#update-serving-settings). Before a response starts, a full queue or expired admission wait returns 503, the total request timeout returns 504, and a batch exceeding capacity returns 400. Enabling limits can reject requests already waiting.

For limits on accepted work at each model instance, see [Instance concurrency](../../docs/features/inference-parameters.md#instance-concurrency). Queue and dispatch metrics are described in [Observability](../../observability/README.md).

### Caller limits and priorities

To give callers different limits, add `roleRules` alongside the waiting limit:

```yaml
spec:
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

Each caller has separate waiting and concurrency allowances shared across frontend replicas. `maxConcurrentRequests` counts dispatched work until execution ends, including cancelled requests still finishing. Tokenization, token counting, and detokenization use waiting capacity only.

Higher `priority` values dispatch first among eligible requests. At equal priority, each frontend rotates between callers and preserves their request order; a caller at its concurrency limit does not block others. To prioritize the vLLM queue as well, set `scheduling-policy: priority` in the model's `engineArgs`.

Role rules require an authentication gateway that overwrites `x-role` and `x-caller-id` with verified role and caller values. Use a stable, non-secret caller ID and restrict frontend access to that gateway. Requests without an authenticated identity or matching role are rejected. Configure cache sharing through [prefix cache isolation](../../docs/features/inference-parameters.md#prefix-cache-isolation).

### Override one model

`ModelService.spec.admission` and `ModelService.spec.roleRules` independently replace the corresponding frontend defaults as whole blocks. Set both `admission: {}` and `roleRules: []` to leave one model unrestricted at the frontend; omitting both at the frontend leaves admission unrestricted by default.

A model's `roleRules[].allowedPools` restricts a role to names in `spec.modelPools`. A disaggregated model must retain a Pool for every required execution stage.

## Update serving settings

Edit the deployment YAML and run `foretoken deploy` again. For example, change logging and request budgets under the existing `spec` in `examples/quickstart/qwen3-0.6b/frontend.yaml`:

```yaml
spec:
  logLevel: debug
  timeouts:
    request: 15m
    streamIdle: 5m
```

Apply the Quick Start configuration:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
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

Stored Responses and Chat Completions expire 30 days after execution ends by default. Conversations use the same retention window, renewed by item or metadata changes and when a turn ends; an active turn keeps its conversation available. Background responses with `store: false` and replay events expire ten minutes after a terminal state.

Platform installation prepares a shared Valkey memory cache. Restarting this cache discards saved text API objects and events; keep history in your application if it must survive cache loss. If only an executing frontend process stops, its response fails without being rerun, while other replicas can still retrieve the cached history.

To customize retention, add `responseStore.retention` and `responseStore.replayRetention` to a [platform values file](../../cli/README.md#installation-options), using durations such as `7d` and `30m`. Apply it with the installation command's `--values` option, then run `foretoken deploy` for the affected service. For an external cache, use a dedicated Valkey or Redis instance with `maxmemory-policy noeviction` and set `responseStore.externalURL`. Authenticated URLs belong in `responseStore.externalURLSecret`, referencing a Secret in each serving namespace. See the [Helm values](../../deploy/charts/foretoken/values.yaml) for these fields.

For gateway configuration, see [Gateway mode](../../README.md#gateway-mode). Configure TLS and authentication at the cluster ingress; network policies govern access to operator endpoints.
