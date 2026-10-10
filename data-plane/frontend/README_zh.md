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

同一 FrontendService 的所有副本按模型共享等待上限，每个调用方分别使用自己的等待和并发限额。等待包含模型就绪和输入准备，并发包含派发预留及后端已接收但尚未结束的生成。批量输入和 `best_of` 按全部生成候选计数。以上数值只是示例，应按实际负载选择。

可派发请求中，`priority` 越大越先处理；同优先级在各前端内按调用方轮转，并保留各调用方的请求顺序。一个调用方并发已满时，其他调用方仍可推进。持续的高优先级流量可能耗尽低优先级请求的等待预算。如需同时启用 vLLM 引擎内的优先级调度，在模型的 `engineArgs` 中设置 `scheduling-policy: priority`。

可信网关根据已认证的调用方覆盖以下请求头，使用稳定标识区分调用方，例如认证密钥的标识：

```http
x-role: role1
x-caller-id: caller-a
```

上面的文本接口共用这两个请求头。启用角色规则后，缺少调用方标识或角色不匹配的请求会被拒绝，客户端指定的调度偏好不能覆盖角色优先级。凭据、授权和 RPM/token 配额由网关管理，前端只应通过部署建立的可信入口访问。分词和 token ID 解码占用等待容量，不占用生成并发；视频接口仍使用独立生命周期。

模型的 `ModelService.spec.admission` 和 `ModelService.spec.roleRules` 分别整块替换对应前端默认配置。模型角色规则中的 `roleRules[].allowedPools` 可引用 `spec.modelPools` 中的名称，限制该角色使用的 Pool；分离式部署必须为每个必需执行阶段保留一个 Pool。要让一个模型在前端不限流，同时设置 `admission: {}` 和 `roleRules: []`；前端默认省略这两项时也不限流。

每个文本模型服务实例还能独立限制已接收但尚未结束的工作，包括引擎内排队的请求。在模型 YAML 中设置 `ModelService.spec.instanceAdmission`；省略此项时，实例接收不限流：

```yaml
spec:
  instanceAdmission:
    maxConcurrentRequests: 32
```

模型程序升级后，后续实例限额调整无需重启模型进程：修改 YAML，再对同一部署目录执行 `foretoken deploy` 即可在线生效。已接收但尚未结束的工作继续占用容量，下调限额不会取消这些工作；设置上限时，已有占用低于当前上限才接收新工作。限额更新不会重新打开正在排空且已关闭接收的实例。

如仍使用 `admission.algorithm` 和 `admission.parameters`，改为上面的等待限额和角色规则，旧字段会被明确拒绝。升级时按常规 `foretoken deploy` 同时部署前端与模型服务；仅升级平台不会替换运行中的应用。

按[修改服务配置](#修改服务配置)应用变更。调整限额会保留已有预留、等待顺序和原等待时限。启用容量限制时，尚未派发且未参与容量计数的请求返回 503。发送响应头前，队列已满或准入等待超时返回 503，请求总时限耗尽返回 504，批次超过容量返回 400。取消请求会通知后端终止，执行结束后才释放并发。

平台安装会准备持久化的共享容量存储。查看等待和派发结果见[可观测性](../../observability/README_zh.md)；扩展请求处理或后端接入见[准入生命周期](../../docs/development/admission-rules_zh.md)。

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

命令等待配置生效后退出。[路由算法及其参数](src/router/README_zh.md)、准入规则、`timeouts.request`、`timeouts.streamIdle` 和 `logLevel` 均可在线更新，不重启前端 Pod。配置无效时，继续使用上一份可用设置。

`timeouts.request` 限制请求总时长，`timeouts.streamIdle` 限制流式响应连续没有数据块的时长，不能大于 `request`。更新后的时限用于新请求，已有请求和流式响应保留原预算。`logLevel` 默认为 `info`，可选 `trace`、`debug`、`info`、`warn`、`error` 或 `off`。

输入长度上限也可在线调整：修改 `ModelService.spec.maxInputTokens`；使用多个 Pool 时，在对应的 `spec.modelPools` 条目中设置 `maxInputTokens`，然后重新部署同一目录。这不会重启模型服务，也不会改变引擎的输入与输出合计上下文上限。

`timeouts.drain` 单独控制前端进程退出时等待已接收请求完成的时长，默认 `10m`。修改它会更新前端 Pod；修改在线请求时限不会。

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
