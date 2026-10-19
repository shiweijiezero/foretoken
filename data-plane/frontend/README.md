<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

English | [简体中文](README_zh.md)

The frontend provides text and video generation APIs for deployed models.

## Generate text

Deploy a service with the repository [Quick Start](../../README.md#quick-start), then send a request from the repository root:

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart/qwen3-0.6b)"

curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","input":"Hello","max_output_tokens":512}'
```

The response contains an `id` and generated `output`. Add `"stream": true` and `curl --no-buffer` to receive output as it is generated. For an authenticated service, add `-H "Authorization: Bearer $API_KEY"` with your issued key. Host-based access follows the [Gateway example](../../README.md#gateway-mode).

| API | Path | Request input |
| --- | --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` | `model`, `messages` |
| OpenAI Responses | `POST /v1/responses` | `model`, `input` |
| Anthropic Messages | `POST /v1/messages` | `model`, `messages`, and required `max_tokens` |
| Anthropic token counting | `POST /v1/messages/count_tokens` | The model, messages, system prompt, and tools to count |
| Text completions | `POST /v1/completions` | `model`, `prompt` |

`GET /v1/models` lists configured model identifiers. `/tokenize` and `/detokenize` convert between text and token IDs. Image-capable text models accept base64 image `data:` URLs.

Tools run in the client, which sends their results in the next request. Responses supports function tools, namespaced functions, and custom-text tools. Forced tool choice and strict schemas require the model's [structured-output capability](#update-serving-settings).

Output budgets include reasoning tokens. Messages uses `max_tokens`, not `thinking.budget_tokens`; thinking controls depend on the model's chat template. Budget exhaustion produces `max_tokens` in Messages or `status: "incomplete"` in Responses. Execute only complete tool calls.

### Continue a conversation

Responses saves results by default. To continue the first request, replace `resp_...` with its returned `id`:

```bash
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","previous_response_id":"resp_...","input":"What did I just say?","max_output_tokens":512}'
```

Send any `instructions` and `tools` needed for the new turn again. Use `store: false` to skip saving the new response.

To keep one conversation ID across turns, create it with `POST /v1/conversations` and an empty `{}` body. Pass the returned ID as `conversation` instead of `previous_response_id`. Each turn reads and updates that history; send turns sequentially. Conversation history is retained even when a turn uses `store: false`.

### Run in the background

Add `background: true` to a Responses request to receive an ID and let generation continue after disconnecting. Set `RESPONSE_ID` to that ID and query the result:

```bash
RESPONSE_ID=resp_...
curl --fail-with-body "$FRONTEND_URL/v1/responses/$RESPONSE_ID"
```

Poll while `status` is `queued` or `in_progress`; the final status is `completed`, `incomplete`, `failed`, or `cancelled`. To cancel, send `POST /v1/responses/{id}/cancel` and wait for a final status. Deleting a response does not cancel generation.

For resumable streaming, create the request with both `background: true` and `stream: true`. Reconnect with `GET /v1/responses/{id}?stream=true&starting_after=N`, using `response.id` from the `response.created` event and the last processed event's `sequence_number` for `N`. Events are available for ten minutes after generation ends by default.

### Manage saved results

Chat Completions saves results only when `store: true` is set. Use the returned ID to retrieve or delete a saved result:

| Saved data | Endpoint | Methods |
| --- | --- | --- |
| Response | `/v1/responses/{id}` | `GET`, `DELETE` |
| Response input history | `/v1/responses/{id}/input_items` | `GET` |
| Conversation | `/v1/conversations/{id}` | `GET`, `POST` metadata, `DELETE` (keeps items) |
| Conversation items | `/v1/conversations/{id}/items` | `GET`, `POST` items |
| One conversation item | `/v1/conversations/{id}/items/{item_id}` | `GET`, `DELETE` |
| Saved Chat Completions | `/v1/chat/completions` | `GET` list |
| One Chat Completion | `/v1/chat/completions/{id}` | `GET`, `POST` metadata, `DELETE` |
| Chat input messages | `/v1/chat/completions/{id}/messages` | `GET` |

Metadata, item references, and pagination follow the [OpenAI API formats](https://platform.openai.com/docs/api-reference). Wait for an active conversation turn to finish before modifying its items.

Responses and saved Chat Completions are kept for 30 days after generation ends by default. Conversations renew that period on each update. Background responses with `store: false` are kept for ten minutes after the request ends by default. Saved history is held in memory and is lost if the cache restarts; keep a copy in your application for permanent retention.

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

`video-results` must be an existing dedicated persistent volume claim (PVC) in the same namespace. Use ReadWriteMany storage across nodes. Place the reference image at `inputs/reference.png` in this volume, then update the recipe's default instance, `h3-api`. If it was deployed with `--name`, set `INSTANCE` to that name instead:

```bash
DEPLOYMENT=examples/recipes/minimax-h3/a100-bf16-tp2
INSTANCE=h3-api
foretoken deploy "$DEPLOYMENT" --name "$INSTANCE" --timeout 1h
FRONTEND_URL="$(foretoken endpoint "$INSTANCE")"
REQUEST_HOST="$(foretoken endpoint "$INSTANCE" --host)"
mkdir -p ./data
```

`modelServiceRef.name` selects the deployed Kubernetes ModelService, not the model identifier. Read this recipe's single ModelService name from the instance before submitting the request. Input paths are relative to the video storage volume:

```bash
MODEL_SERVICE="$(kubectl get inferencedeployment "$INSTANCE" \
  -o jsonpath='{.spec.resources[?(@.kind=="ModelService")].name}')"

curl --fail-with-body "$FRONTEND_URL/v1/videos" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<EOF
{
  "modelServiceRef": {"name": "$MODEL_SERVICE"},
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
}
EOF
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

The waiting limit is shared across frontend replicas for each model, including input preparation. `queueTimeout` bounds waiting and preparation. Batched prompts and `best_of` count each generated sequence. These rules apply to text APIs.

Apply changes with `foretoken deploy`. Before a response starts, a full queue or expired admission wait returns 503, the total request timeout returns 504, and a batch exceeding capacity returns 400. Enabling limits can reject requests already waiting. For accepted work, see [Instance concurrency](../../docs/features/inference-parameters.md#instance-concurrency).

### Caller limits and priorities

To give callers different limits, add `roleRules`:

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

Each caller has separate waiting and concurrency allowances shared across replicas. Concurrency includes cancelled work until execution ends; tokenization and token counting use waiting capacity only. Higher `priority` values dispatch first among eligible requests. To prioritize the vLLM queue as well, set `scheduling-policy: priority` in the model's `engineArgs`.

Role rules require an authentication gateway that overwrites `x-role` and `x-caller-id` with verified values. Use a stable, non-secret caller ID and restrict frontend access to that gateway. Requests without an authenticated identity or matching role are rejected. See also [prefix cache isolation](../../docs/features/inference-parameters.md#prefix-cache-isolation).

### Override one model

`ModelService.spec.admission` and `ModelService.spec.roleRules` replace the corresponding frontend defaults as whole blocks. Set `admission: {}` and `roleRules: []` to leave one model unrestricted at the frontend; omitting both at the frontend leaves admission unrestricted by default.

A model's `roleRules[].allowedPools` restricts a role to names in `spec.modelPools`. A disaggregated model must retain a Pool for every required execution stage.

## Update serving settings

Edit the deployment's `frontend.yaml` and redeploy. For example:

```yaml
spec:
  logLevel: debug
  timeouts:
    request: 15m
    streamIdle: 5m
```

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

[Routing strategies](src/router/README.md), admission rules, logging, and request timeouts update online. Timeout changes apply to new requests; invalid settings leave the working configuration unchanged.

| Setting | Purpose |
| --- | --- |
| `timeouts.request` | Total request duration |
| `timeouts.streamIdle` | Maximum gap between response chunks; must not exceed `request` |
| `timeouts.drain` | Shutdown wait for accepted requests, default `10m`; changing it replaces frontend Pods |
| `logLevel` | `trace`, `debug`, `info` (default), `warn`, `error`, or `off` |

To limit prompt length, set `ModelService.spec.maxInputTokens`, or `spec.modelPools[].maxInputTokens` for individual Pools. This updates online and is separate from the engine's combined input/output context limit.

Models whose strict tool calls require structural tags must declare that capability in the model YAML:

```yaml
spec:
  features:
    structuredOutputs: [structuralTag]
```

With `spec.modelPools`, add it to each applicable Pool's `features.structuredOutputs` instead. Redeploy the model configuration after changing model capabilities.

## Operations

Pass the deployment instance name to `foretoken status` to inspect it, or to `foretoken delete` to remove it. Both commands also accept its configuration directory, with the original `--name` when one was selected. Metrics, logs, and alerts are covered by [Observability](../../observability/README.md).

| Endpoint | Purpose |
| --- | --- |
| `/healthz` | Process liveness |
| `/readyz` | Frontend HTTP readiness |
| `/statusz` | Serving readiness in `serving_ready` |
| `/metrics` | Prometheus metrics |

Retention and external-cache options are under `responseStore` in the [Helm values](../../deploy/charts/foretoken/values.yaml). Apply platform changes with [install](../../cli/README.md#install), then redeploy the service.
