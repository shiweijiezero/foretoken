<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 前端

[English](README.md) | 简体中文

前端为已部署的模型提供文本和视频生成接口。

## 文本生成

按仓库[快速开始](../../README_zh.md#快速开始)完成部署，再从仓库根目录发送请求：

```bash
DEPLOYMENT=examples/quickstart
FRONTEND_URL="$(foretoken endpoint "$DEPLOYMENT")"
REQUEST_HOST="$(foretoken endpoint "$DEPLOYMENT" --host)"

# OpenAI Responses
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","input":"你好","max_output_tokens":512,"store":false}'

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
| OpenAI Responses | `POST /v1/responses` | `model`、`input`；设置 `store: false`，每轮携带对话历史 |
| Anthropic Messages | `POST /v1/messages` | `model`、`messages`、必填的 `max_tokens` |
| Anthropic token 计数 | `POST /v1/messages/count_tokens` | 与生成请求一致的模型、消息、系统提示和工具定义 |
| 文本补全 | `POST /v1/completions` | `model`、`prompt` |

`GET /v1/models` 列出已配置的模型标识；`/tokenize` 和 `/detokenize` 用于文本与 token ID 之间的转换。支持图片的文本模型接受 base64 编码的图片 `data:` URL。

工具由客户端执行，再将结果传入下一轮请求。Responses 支持函数工具、带命名空间的函数和自定义文本工具，不支持服务端托管工具或后台执行模式。强制选择工具和严格约束工具参数需要模型支持结构化输出。

部分工具解析器通过结构标签语法约束强制工具调用或严格工具的输出格式。模型的解析器和语法后端支持该能力时，在 ModelService 已有的结构化输出格式中加入 `structuralTag`：

```yaml
spec:
  features:
    structuredOutputs: [structuralTag]
```

使用 `spec.modelPools` 配置时，在适用池的 `features.structuredOutputs` 中声明该能力，不使用顶层 `features`。

输出 token 预算包含思考内容。Messages 使用 `max_tokens`，不接受独立的 `thinking.budget_tokens`；思考控制取决于模型的聊天模板。预算耗尽时，Messages 返回 `max_tokens`，Responses 返回 `incomplete`，客户端只应执行完整的工具调用。

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

准入为文本生成设置等待容量和调用方并发上限。在部署的 `frontend.yaml` 已有 `spec` 下添加：

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

同一 FrontendService 的所有副本按模型共享这些限额，每个调用方分别计数。`maxWaitingRequests` 限制等待数量，包括正在准备输入的请求；`maxConcurrentRequests` 限制已派发但尚未结束的工作数量；`queueTimeout` 限制后端接收前的等待与输入准备时长。批量输入和 `best_of` 按每个生成候选计数。

`priority` 越大越先派发。同优先级时，各前端在调用方之间轮流派发，保持每个调用方的请求顺序。如需同时启用 vLLM 队列中的优先级调度，在模型的 `engineArgs` 中设置 `scheduling-policy: priority`。

可信网关根据已认证的调用方覆盖以下请求头，使用稳定标识区分调用方，例如认证密钥的标识：

```http
x-role: role1
x-caller-id: caller-a
```

所有文本接口共用这两个请求头。启用角色规则后，缺少调用方标识或角色不匹配的请求会被拒绝。请在网关配置身份认证、授权和 RPM/token 配额，并将前端访问限制为该可信网关。文本与 token ID 转换只占等待容量；这些规则不适用于视频接口。

模型的 `ModelService.spec.admission` 和 `ModelService.spec.roleRules` 分别整块替换对应前端默认配置。模型角色规则中的 `roleRules[].allowedPools` 可引用 `spec.modelPools` 中的名称，限制该角色使用的 Pool；分离式部署必须为每个必需执行阶段保留一个 Pool。要让一个模型在前端不限流，同时设置 `admission: {}` 和 `roleRules: []`；前端默认省略这两项时也不限流。

要限制每个文本模型服务实例可以接收的未完成请求数，请在模型 YAML 中加入 `instanceAdmission`。引擎内排队的请求也计入此上限；省略此项表示不限流：

```yaml
spec:
  instanceAdmission:
    maxConcurrentRequests: 32
```

升级平台后，先执行一次 `foretoken deploy` 选择当前模型程序。之后修改实例限额会在线生效。已有工作继续完成；达到上限时，新请求返回 `503`，直到占用降到上限以下。

如仍使用 `admission.algorithm` 和 `admission.parameters`，改为上面的等待限额和角色规则，旧字段会被明确拒绝。升级时按常规 `foretoken deploy` 同时部署前端与模型服务；仅升级平台不会替换运行中的应用。

按[修改服务配置](#修改服务配置)应用变更。响应开始前，队列已满或等待超时返回 503，请求总超时返回 504，批次超过容量返回 400。启用限额时，已在等待的请求可能被拒绝。取消的请求在执行结束前仍计入并发。

查看等待和派发结果见[可观测性](../../observability/README_zh.md)。维护者扩展请求处理或后端接入时，可参考[准入生命周期](../../docs/development/admission-rules_zh.md)。

## 修改服务配置

修改部署 YAML 后，再执行 `foretoken deploy` 即可。例如，在 `examples/quickstart/frontend.yaml` 已有的 `spec` 中调整日志级别和请求时限：

```yaml
spec:
  logLevel: debug
  timeouts:
    request: 15m
    streamIdle: 5m
```

重新部署快速开始示例：

```bash
foretoken deploy examples/quickstart --timeout 20m
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

网关配置见[网关模式](../../README_zh.md#网关模式)。TLS 和身份认证由集群入口配置，运维接口的访问范围由网络策略控制。
