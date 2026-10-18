<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 前端

[English](README.md) | 简体中文

前端为已部署的模型提供文本和视频生成接口。

## 文本生成

按仓库[快速开始](../../README_zh.md#快速开始)完成部署，再从仓库根目录发送请求。网关使用 API Key 认证时，将它签发的密钥设为 `API_KEY`，并为以下每个请求添加 `-H "Authorization: Bearer $API_KEY"`。

```bash
DEPLOYMENT=examples/quickstart/qwen3-0.6b
FRONTEND_URL="$(foretoken endpoint "$DEPLOYMENT")"
REQUEST_HOST="$(foretoken endpoint "$DEPLOYMENT" --host)"

# OpenAI Responses
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","input":"你好","max_output_tokens":512}'

# Anthropic Messages
curl --fail-with-body "$FRONTEND_URL/v1/messages" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"max_tokens":512}'
```

添加 `"stream": true` 和 `curl --no-buffer` 可实时接收生成内容。

| 接口 | 路径 | 请求内容 |
| --- | --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` | `model`、`messages` |
| OpenAI Responses | `POST /v1/responses` | `model`、`input` |
| Anthropic Messages | `POST /v1/messages` | `model`、`messages`、必填的 `max_tokens` |
| Anthropic token 计数 | `POST /v1/messages/count_tokens` | 与生成请求一致的模型、消息、系统提示和工具定义 |
| 文本补全 | `POST /v1/completions` | `model`、`prompt` |

`GET /v1/models` 列出已配置的模型标识；`/tokenize` 和 `/detokenize` 用于文本与 token ID 之间的转换。支持图片的文本模型接受 base64 编码的图片 `data:` URL。

工具由客户端执行，再将结果传入下一轮请求。Responses 支持函数工具、带命名空间的函数和自定义文本工具，不支持服务端托管工具。强制选择工具和严格约束工具参数需要模型支持结构化输出。

部分工具解析器通过结构标签语法约束强制工具调用或严格工具的输出格式。模型的解析器和语法后端支持该能力时，在 ModelService 已有的结构化输出格式中加入 `structuralTag`：

```yaml
spec:
  features:
    structuredOutputs: [structuralTag]
```

使用 `spec.modelPools` 配置时，在适用池的 `features.structuredOutputs` 中声明该能力，不使用顶层 `features`。

输出 token 预算包含思考内容。Messages 使用 `max_tokens`，不接受独立的 `thinking.budget_tokens`；思考控制取决于模型的聊天模板。预算耗尽时，Messages 返回 `max_tokens`；Responses 的 `status` 为 `incomplete`，`incomplete_details.reason` 为 `max_output_tokens`。Responses 的 JSON 响应、SSE 终态事件和保存后取回的结果具有一致的最终状态、输出条目和用量。客户端只应执行完整的工具调用。

### 延续对话

Responses 默认使用 `store: true`，将响应保存在同一 FrontendService 各副本共享的内存缓存中。继续上面的第一轮对话时，将返回的 `id` 填入 `RESPONSE_ID`，替换示例中的 `resp_...`，下一轮只需发送新的输入：

```bash
RESPONSE_ID=resp_...
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"Qwen/Qwen3-0.6B\",\"previous_response_id\":\"$RESPONSE_ID\",\"input\":\"我刚才说了什么？\",\"max_output_tokens\":512}"
```

新一轮仍需使用的 `instructions` 和 `tools` 应重新传入。如只需复用某个已保存条目，将 `input` 设为 `[{"type":"item_reference","id":"..."}]`；条目 ID 来自 `output[].id`，或输入条目列表中的 `data[].id`。

| 操作 | 接口 |
| --- | --- |
| 取回状态和输出 | `GET /v1/responses/{id}` |
| 列出展开后的输入历史 | `GET /v1/responses/{id}/input_items` |
| 删除保存的响应 | `DELETE /v1/responses/{id}` |

如需保存并管理整段对话，先创建一个 conversation：

```bash
curl --fail-with-body "$FRONTEND_URL/v1/conversations" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{}'
```

将返回的 `id` 填入 `CONVERSATION_ID`，替换 `conv_...`。请求改用 `conversation`，不再同时传入 `previous_response_id`：

```bash
CONVERSATION_ID=conv_...
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"Qwen/Qwen3-0.6B\",\"conversation\":\"$CONVERSATION_ID\",\"input\":\"你好\",\"max_output_tokens\":512,\"store\":false}"
```

每轮会读取 conversation 历史，并追加本轮输入和输出。这里的 `store: false` 只关闭响应存档，不关闭对话历史写入。开始下一轮或修改条目前，先等待当前响应结束。完全由客户端管理历史时，使用 `store: false`，不传 `conversation` 或 `previous_response_id`，每轮携带所需的历史输入和输出条目。

| 操作 | 接口 |
| --- | --- |
| 获取 conversation，或用 `{"metadata":{...}}` 替换元数据 | `GET` 或 `POST /v1/conversations/{id}` |
| 删除容器，保留其中的条目 | `DELETE /v1/conversations/{id}` |
| 列出条目，或用 `{"items":[...]}` 追加条目 | `GET` 或 `POST /v1/conversations/{id}/items` |
| 获取条目，或将其移出 conversation | `GET` 或 `DELETE /v1/conversations/{id}/items/{item_id}` |

条目列表支持 `limit`、`order`，以及使用条目 ID 的 `after` 或 `before` 游标。

### 后台执行 Responses

设置 `background: true`，请求会先返回响应 ID，生成过程不依赖 HTTP 连接继续执行：

```bash
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","input":"解释彩虹是怎样形成的。","max_output_tokens":512,"background":true}'
```

将 `RESPONSE_ID` 更新为这次返回的 `id`，再轮询：

```bash
curl --fail-with-body "$FRONTEND_URL/v1/responses/$RESPONSE_ID" \
  -H "Host: $REQUEST_HOST"
```

`status` 为 `queued` 或 `in_progress` 时继续轮询；`completed`、`incomplete`、`failed` 和 `cancelled` 为终态。需要取消时，发送 `POST /v1/responses/{id}/cancel`，再轮询至执行结束。删除响应不会取消执行。后台请求仍遵循服务的准入规则和请求时限。

需要断线后恢复 SSE 时，创建请求时同时设置 `background: true` 和 `stream: true`，并使用 `curl --no-buffer`。将 `response.created` 事件中 `response.id` 保存为 `RESPONSE_ID`，把下面的 `42` 替换为客户端最后处理的事件 `sequence_number`：

```bash
SEQUENCE_NUMBER=42
curl --fail-with-body --no-buffer \
  "$FRONTEND_URL/v1/responses/$RESPONSE_ID?stream=true&starting_after=$SEQUENCE_NUMBER" \
  -H "Host: $REQUEST_HOST"
```

恢复连接只接收该序号之后的事件，并继续等待尚未完成的生成，不会重新执行请求。原请求必须同时开启上述两个选项，且事件尚在[保留期](#运维)内。

### 保存 Chat Completions

Chat Completions 默认使用 `store: false`。设置 `store: true` 后，最终结果与原始输入消息会被保存，JSON 和流式请求均适用：

```bash
curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"max_tokens":512,"store":true}'
```

将下表的 `{id}` 替换为返回的 completion `id`：

| 操作 | 接口 |
| --- | --- |
| 列出存档，可按 `model` 或 `metadata[key]` 筛选 | `GET /v1/chat/completions` |
| 获取最终结果 | `GET /v1/chat/completions/{id}` |
| 列出原始输入消息 | `GET /v1/chat/completions/{id}/messages` |
| 用 `{"metadata":{...}}` 替换元数据 | `POST /v1/chat/completions/{id}` |
| 删除存档 | `DELETE /v1/chat/completions/{id}` |

## 视频生成

通过 `POST /v1/videos/sync` 提交提示词和参考图片或视频，生成完成后直接返回视频内容。[MiniMax H3 配方](../../examples/recipes/minimax-h3/a100-bf16-tp2/README_zh.md#根据图片生成视频)提供完整的部署和请求命令，将结果保存到 `./data/video.mp4`。

### 后台生成

如需提交后离开、稍后获取结果，在部署的 `frontend.yaml` 中启用：

```yaml
spec:
  videoTasks:
    claimName: video-results
    retentionSeconds: 86400
```

`video-results` 对应同一命名空间内已准备好的专用持久化存储声明（PVC）；跨节点运行时需支持 ReadWriteMany 共享访问。将参考图片放入该卷的 `inputs/reference.png`，然后应用配置：

```bash
DEPLOYMENT=examples/recipes/minimax-h3/a100-bf16-tp2
foretoken deploy "$DEPLOYMENT" --timeout 1h
FRONTEND_URL="$(foretoken endpoint "$DEPLOYMENT")"
REQUEST_HOST="$(foretoken endpoint "$DEPLOYMENT" --host)"
mkdir -p ./data
```

以下请求选择名为 `h3` 的模型服务，输入路径相对于视频存储卷根目录：

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

响应包含任务 `id`、状态地址 `status_url` 和视频地址 `content_url`。继续使用同一个前端地址，将下表的 `{id}` 替换为返回的任务 ID：

| 操作 | 接口 | 说明 |
| --- | --- | --- |
| 查看状态 | `GET /v1/videos/{id}` | 等待 `phase` 为 `Succeeded`；`Failed` 时查看 `reason` 和 `message` |
| 保存视频 | `GET /v1/videos/{id}/content` | 生成成功后，用 `curl --output ./data/video.mp4` 保存响应 |
| 取消任务 | `POST /v1/videos/{id}/cancel` | 请求取消，后端计算可能仍在结束中 |
| 删除任务 | `DELETE /v1/videos/{id}` | 清理任务及其文件 |

取消和删除请求返回 `202` 后继续由服务处理。以上配置保留结果一天，从任务结束起算；到期后自动清理，原始参考文件保留。

## 配置准入规则

要限制等待中的文本请求，在部署的 `frontend.yaml` 已有 `spec` 下添加 `admission`：

```yaml
spec:
  admission:
    maxWaitingRequests: 128
    queueTimeout: 30s
```

同一 FrontendService 的所有副本按模型共享等待上限，正在准备输入的请求也计入其中。`queueTimeout` 限制后端接收前的等待与准备时长。批量输入和 `best_of` 按生成序列计数；这些规则不适用于视频接口。

按[修改服务配置](#修改服务配置)应用 YAML。响应开始前，队列已满或准入等待超时返回 503，请求总超时返回 504，批次超过容量返回 400。启用限额时，已在等待的请求可能被拒绝。

限制单个模型实例已接收的工作，见[实例并发上限](../../docs/features/inference-parameters_zh.md#实例并发上限)；查看等待和派发指标，见[可观测性](../../observability/README_zh.md)。

### 调用方限额与优先级

需要区分调用方时，在等待上限之外添加 `roleRules`：

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

每个调用方分别计量等待和并发数量，跨前端副本共享。`maxConcurrentRequests` 统计已派发但尚未结束的工作，取消后仍在执行的请求也计入其中。分词、token 计数和 token ID 转文本只占等待容量。

可派发请求中，`priority` 越大越先派发；同优先级在各前端内按调用方轮转，保持调用方内部的请求顺序。并发已满的调用方不阻塞其他调用方。如需同时启用 vLLM 队列的优先级调度，在模型的 `engineArgs` 中设置 `scheduling-policy: priority`。

角色规则需要认证网关校验身份，并覆盖 `x-role` 和 `x-caller-id` 后转发。调用方标识使用稳定、非秘密值，前端只允许该网关访问。缺少已认证身份或角色不匹配的请求会被拒绝。缓存共享方式见[前缀缓存隔离](../../docs/features/inference-parameters_zh.md#前缀缓存隔离)。

### 为单个模型覆盖配置

`ModelService.spec.admission` 和 `ModelService.spec.roleRules` 分别整块替换对应的前端默认配置。要让一个模型在前端不限流，同时设置 `admission: {}` 和 `roleRules: []`；前端省略这两项时默认不限流。

模型角色规则中的 `roleRules[].allowedPools` 通过 `spec.modelPools` 的名称限制可用 Pool。分离式部署必须为每个必需执行阶段保留一个 Pool。

## 修改服务配置

修改部署 YAML 后，再执行 `foretoken deploy` 即可。例如，在 `examples/quickstart/qwen3-0.6b/frontend.yaml` 已有的 `spec` 中调整日志级别和请求时限：

```yaml
spec:
  logLevel: debug
  timeouts:
    request: 15m
    streamIdle: 5m
```

重新部署快速开始示例：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

命令等待配置生效后退出。[路由策略](src/router/README_zh.md)、准入规则、日志级别和请求时限支持在线更新；配置无效时保留原设置。

| 设置 | 用途 |
| --- | --- |
| `timeouts.request` | 请求总时长 |
| `timeouts.streamIdle` | 响应数据块之间的最大间隔，不超过 `request` |
| `timeouts.drain` | 退出时等待已接收请求完成的时长，默认 `10m`；修改它会替换前端 Pod |
| `logLevel` | 可选 `trace`、`debug`、`info`（默认）、`warn`、`error`、`off` |

请求时限的修改用于新请求。限制输入长度时，设置 `ModelService.spec.maxInputTokens`，或对应的 `spec.modelPools[].maxInputTokens`，再重新部署。该设置支持在线更新，与引擎的输入输出合计上下文上限分别配置。

## 运维

使用 `foretoken status` 查看部署状态，使用 `foretoken delete` 删除部署，均传入对应配置目录。

| 接口 | 用途 |
| --- | --- |
| `/healthz` | 进程存活状态 |
| `/readyz` | 已加载有效路由配置、可以接收 HTTP 请求 |
| `/statusz` | 服务和缓存索引状态 |
| `/metrics` | Prometheus 指标 |

模型启动或切换时，HTTP 前端保持可访问。合法的空模型配置也可接收 HTTP 请求，此时推理请求返回 HTTP 503。服务就绪状态见 `/statusz` 中的 `serving_ready`。

保存的 Responses 和 Chat Completions 默认从执行结束起保留 30 天。Conversation 使用相同保留期，修改条目、更新元数据或一轮执行结束后重新计时；正在执行的对话保持可用。`store: false` 的后台响应及流式重放事件保留至请求进入终态后的十分钟。

平台安装会自动准备共享的 Valkey 内存缓存。缓存重启后，已保存的文本 API 对象和事件会丢失；需要长期保留的历史应由应用自行保存。如果只是负责执行的前端进程停止，本次响应会失败且不重新生成，其他副本仍可读取缓存中的历史。

需要调整保留期时，在[平台 values 文件](../../cli/README_zh.md#安装选项)中设置 `responseStore.retention` 和 `responseStore.replayRetention`，使用 `7d`、`30m` 等带单位的时长。通过安装命令的 `--values` 选项应用，再对需要更新的服务执行 `foretoken deploy`。外部缓存使用独立的 Valkey 或 Redis 实例，将 `maxmemory-policy` 设为 `noeviction`，并通过 `responseStore.externalURL` 指定地址。含凭据的 URL 通过 `responseStore.externalURLSecret` 引用，该 Secret 需存在于每个服务命名空间。字段说明见 [Helm values](../../deploy/charts/foretoken/values.yaml)。

网关配置见[网关模式](../../README_zh.md#网关模式)。TLS 和身份认证由集群入口配置，运维接口的访问范围由网络策略控制。
