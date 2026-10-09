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

准入规则控制文本生成和 tokenization 的并发与排队，默认不限流（`allow_all`）。通过 `FrontendService.spec.admission` 为各模型设置默认规则，例如：

```yaml
spec:
  admission:
    algorithm: concurrency
    parameters:
      maxConcurrentRequests: 64
```

限额在每个前端副本内按模型独立生效，各模型不共享前端总上限或队列。并发上限按实际负载选择，批量请求按输出候选数计数。需要排队时，在 `parameters` 下添加 `maxQueuedRequests`，并可用 `queueTimeout` 设置等待时限。

模型的 `ModelService.spec.admission` 整块替换前端默认规则，不合并参数。例如，让某个模型保持不限流：

```yaml
spec:
  admission:
    algorithm: allow_all
```

重新部署服务配置后生效。准入规则更新和模型增删不会重启前端 Pod；模型更换规则期间，新请求可能返回 HTTP 503。

查看准入结果见[可观测性](../../observability/README_zh.md)，新增算法见[开发准入规则](../../docs/development/admission-rules_zh.md)。


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
