<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 前端

[English](README.md) | 简体中文

前端为已部署的模型提供文本和视频生成接口。

## 文本生成

按仓库[快速开始](../../README_zh.md#快速开始)完成部署后，从仓库根目录发送请求：

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart/qwen3-0.6b)"
REQUEST_HOST="$(foretoken endpoint examples/quickstart/qwen3-0.6b --host)"

curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","input":"你好","max_output_tokens":512}'
```

响应包含 `id` 和生成结果 `output`。添加 `"stream": true` 和 `curl --no-buffer` 可实时接收输出。服务启用认证时，使用已签发的密钥添加 `-H "Authorization: Bearer $API_KEY"`；自定义域名和 HTTPS 的设置见[网关配置](../../README_zh.md#网关模式)。

| 接口 | 路径 | 请求内容 |
| --- | --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` | `model`、`messages` |
| OpenAI Responses | `POST /v1/responses` | `model`、`input` |
| Anthropic Messages | `POST /v1/messages` | `model`、`messages`、必填的 `max_tokens` |
| Anthropic token 计数 | `POST /v1/messages/count_tokens` | 需要计数的模型、消息、系统提示和工具定义 |
| 文本补全 | `POST /v1/completions` | `model`、`prompt` |

`GET /v1/models` 列出已配置的模型；`/tokenize` 和 `/detokenize` 用于文本与 token ID 之间的转换。支持图片的文本模型接受 base64 编码的图片 `data:` URL。

工具由客户端执行，再将结果传入下一轮请求。Responses 支持函数工具、带命名空间的函数和自定义文本工具。强制选择工具和严格约束参数需要模型具备[结构化输出能力](#修改服务配置)。

输出 token 预算包含思考内容。Chat Completions 支持 `max_completion_tokens` 和 `max_tokens`，同时传入时以前者为准。Messages 使用 `max_tokens`，不接受 `thinking.budget_tokens`；思考控制取决于模型的聊天模板。预算耗尽时，Messages 返回 `max_tokens`，Responses 返回 `status: "incomplete"`。客户端只应执行完整的工具调用。

### 延续对话

Responses 默认保存结果。继续第一轮对话时，将下面的 `resp_...` 替换为刚返回的 `id`：

```bash
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","previous_response_id":"resp_...","input":"我刚才说了什么？","max_output_tokens":512}'
```

新一轮需要的 `instructions` 和 `tools` 仍需传入。设置 `store: false` 可不保存这次响应。

需要用同一个 ID 管理多轮对话时，以空的 `{}` 请求体调用 `POST /v1/conversations`，再将返回的 ID 填入 `conversation`，替代 `previous_response_id`。每轮都会读取和更新对话历史，请依次发送各轮请求。即使某轮设置了 `store: false`，对话历史仍会保留。

### 后台执行

在 Responses 请求中添加 `background: true`，接口会先返回 ID，断开连接后仍继续生成。将 `RESPONSE_ID` 设为这次返回的 ID，即可查询结果：

```bash
RESPONSE_ID=resp_...
curl --fail-with-body "$FRONTEND_URL/v1/responses/$RESPONSE_ID" \
  -H "Host: $REQUEST_HOST"
```

`status` 为 `queued` 或 `in_progress` 时继续轮询，最终状态为 `completed`、`incomplete`、`failed` 或 `cancelled`。需要取消时，调用 `POST /v1/responses/{id}/cancel`，再等待最终状态。删除响应不会取消生成。

需要断线续传时，创建请求时同时设置 `background: true` 和 `stream: true`。重连使用 `GET /v1/responses/{id}?stream=true&starting_after=N`：`{id}` 取自 `response.created` 事件的 `response.id`，`N` 为最后处理的事件 `sequence_number`。流式事件默认保留至生成结束后的十分钟。

### 管理已保存的结果

Chat Completions 需要设置 `store: true` 才会保存。用返回的 ID 查询或删除结果：

| 保存的数据 | 接口 | 方法 |
| --- | --- | --- |
| Response | `/v1/responses/{id}` | `GET`、`DELETE` |
| Response 输入历史 | `/v1/responses/{id}/input_items` | `GET` |
| 对话 | `/v1/conversations/{id}` | `GET`、`POST` 更新元数据、`DELETE`（保留条目） |
| 对话条目 | `/v1/conversations/{id}/items` | `GET`、`POST` 添加条目 |
| 单个对话条目 | `/v1/conversations/{id}/items/{item_id}` | `GET`、`DELETE` |
| Chat Completions 列表 | `/v1/chat/completions` | `GET` |
| 单条 Chat Completion | `/v1/chat/completions/{id}` | `GET`、`POST` 更新元数据、`DELETE` |
| Chat 输入消息 | `/v1/chat/completions/{id}/messages` | `GET` |

元数据、条目引用和分页使用 [OpenAI API 格式](https://platform.openai.com/docs/api-reference)。修改对话条目前，先等待正在生成的这一轮结束。

Responses 和保存的 Chat Completions 默认从生成结束起保留 30 天；对话的保留期在每次更新后重新计算。设置 `store: false` 的后台响应默认从请求结束起保留十分钟。历史保存在内存中，缓存重启会使其丢失；需要永久保留时，由应用保存一份副本。

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

`video-results` 对应同一命名空间内已准备好的专用持久化存储声明（PVC）；跨节点运行时需支持 ReadWriteMany 共享访问。将参考图片放入该卷的 `inputs/reference.png`，再更新配方的默认实例 `h3-api`。如果部署时指定了 `--name`，将 `INSTANCE` 换成原来的实例名：

```bash
DEPLOYMENT=examples/recipes/minimax-h3/a100-bf16-tp2
INSTANCE=h3-api
foretoken deploy "$DEPLOYMENT" --name "$INSTANCE" --timeout 1h
FRONTEND_URL="$(foretoken endpoint "$INSTANCE")"
REQUEST_HOST="$(foretoken endpoint "$INSTANCE" --host)"
mkdir -p ./data
```

`model` 使用 `GET /v1/models` 列出的公开模型标识。后台生成要求它在部署中唯一对应一个视频服务。输入路径相对于视频存储卷根目录：

```bash
curl --fail-with-body "$FRONTEND_URL/v1/videos" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'EOF'
{
  "model": "MiniMaxAI/MiniMax-H3",
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

每个模型的等待上限跨前端副本共享，包含正在准备输入的请求。`queueTimeout` 限制等待与准备时长；批量输入和 `best_of` 按生成序列计数。这些规则用于文本接口。

通过 `foretoken deploy` 应用修改。响应开始前，队列已满或准入等待超时返回 503，请求总超时返回 504，批次超过容量返回 400。启用限额时，已在等待的请求可能被拒绝。限制已接收的工作，见[实例并发上限](../../docs/features/inference-parameters_zh.md#实例并发上限)。

### 调用方限额与优先级

需要区分调用方时，添加 `roleRules`：

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

每个调用方分别计量等待和并发数量，跨副本共享。取消后尚未结束的工作仍计入并发；分词和 token 计数只占等待容量。可派发请求中，`priority` 越大越先派发。如需同时启用 vLLM 队列的优先级调度，在模型的 `engineArgs` 中设置 `scheduling-policy: priority`。

角色规则需要认证网关校验身份，并覆盖 `x-role` 和 `x-caller-id` 后转发。调用方标识使用稳定、非秘密值，前端只允许该网关访问。缺少已认证身份或角色不匹配的请求会被拒绝。另见[前缀缓存隔离](../../docs/features/inference-parameters_zh.md#前缀缓存隔离)。

### 为单个模型覆盖配置

`ModelService.spec.admission` 和 `ModelService.spec.roleRules` 分别整块替换对应的前端默认配置。要让一个模型在前端不限流，设置 `admission: {}` 和 `roleRules: []`；前端省略这两项时默认不限流。

模型角色规则中的 `roleRules[].allowedPools` 通过 `spec.modelPools` 的名称限制可用 Pool。分离式部署必须为每个必需执行阶段保留一个 Pool。

## 修改服务配置

修改部署的 `frontend.yaml` 后重新部署。例如：

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

[路由策略](src/router/README_zh.md)、准入规则、日志级别和请求时限支持在线更新。时限修改用于新请求，配置无效时保留原设置。

| 设置 | 用途 |
| --- | --- |
| `timeouts.request` | 请求总时长 |
| `timeouts.streamIdle` | 响应数据块之间的最大间隔，不超过 `request` |
| `timeouts.drain` | 退出时等待已接收请求完成的时长，默认 `10m`；修改它会替换前端 Pod |
| `logLevel` | `trace`、`debug`、`info`（默认）、`warn`、`error`、`off` |

限制输入长度时，设置 `ModelService.spec.maxInputTokens`，或各 Pool 的 `spec.modelPools[].maxInputTokens`。该设置支持在线更新，与引擎的输入输出合计上下文上限分别配置。

模型的严格工具调用需要结构标签时，在模型 YAML 中声明该能力：

```yaml
spec:
  features:
    structuredOutputs: [structuralTag]
```

使用 `spec.modelPools` 时，改为设置适用 Pool 的 `features.structuredOutputs`。修改模型能力后，重新部署模型配置。

## 运维

用 `foretoken status` 查看部署实例，`foretoken delete` 删除实例，参数传入对应实例名。也可以传入配置目录；部署时使用了 `--name` 的，操作时沿用该名称。指标、日志和告警见[可观测性指南](../../observability/README_zh.md)。

前端 Service 在集群内提供以下运维接口：

| 接口 | 用途 |
| --- | --- |
| `/healthz` | 进程存活状态 |
| `/readyz` | 前端 HTTP 就绪状态 |
| `/statusz` | `serving_ready` 表示推理服务是否就绪 |
| `/metrics` | Prometheus 指标 |

保留期和外部缓存选项位于 [Helm values](../../deploy/charts/foretoken/values.yaml) 的 `responseStore` 下。通过 [install](../../cli/README_zh.md#install) 应用平台配置，再重新部署服务。
