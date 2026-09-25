<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

前端在同一地址提供 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 接口，通过请求中的 `model` 选择已配置的模型。

## 发送请求

按仓库[快速开始](../../README_zh.md#快速开始)完成部署，其中已有 Chat Completions 调用示例。从仓库根目录执行以下命令，即可通过同一部署调用 Responses 和 Messages：

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

# OpenAI Responses
curl --fail-with-body "$FRONTEND_URL/v1/responses" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","input":"你好","max_output_tokens":512,"store":false}'

# Anthropic Messages
curl --fail-with-body "$FRONTEND_URL/v1/messages" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"max_tokens":512}'
```

以上请求返回 JSON。在请求中添加 `"stream": true` 可逐步接收服务端事件流（SSE）；为 `curl` 添加 `--no-buffer` 可实时显示输出。

## 选择接口

| API | POST 路径 | 对话输入 |
| --- | --- | --- |
| OpenAI Chat Completions | `/v1/chat/completions` | `messages` |
| OpenAI Responses | `/v1/responses` | `input`；设置 `store: false`，每轮携带完整对话历史 |
| Anthropic Messages | `/v1/messages` | `messages`，并用必填的 `max_tokens` 指定输出预算 |
| Anthropic token 计数 | `/v1/messages/count_tokens` | `messages`，以及与生成请求一致的系统提示和工具定义 |

通过 `GET /v1/models` 查看模型标识。文本补全使用 `POST /v1/completions`；`/tokenize` 和 `/detokenize` 用于文本与 token ID 之间的转换。

工具由客户端执行，再将结果传入下一轮请求。Responses 支持函数工具、带命名空间的函数和无语法约束的自定义文本工具；不支持服务端托管工具和后台生成任务。

强制工具选择和严格工具 schema 需要模型服务支持结构化输出。思考控制参数由模型的对话模板支持。输出预算包含思考 token；Messages 使用 `max_tokens` 指定总预算，不接受独立的 `thinking.budget_tokens` 预算。

输出预算耗尽时，Messages 返回 `max_tokens`，Responses 返回 `incomplete`。只执行完整的工具调用；中断的调用可能被省略，也可能包含未完成的参数。

支持图片的模型服务接受 base64 编码的图片 `data:` URL，而非远程图片 URL。

## 访问与运维

默认通过 Kubernetes LoadBalancer 访问前端。使用域名访问时，参阅[网关模式](../../README_zh.md#网关模式)；TLS 和认证在集群入口配置。

| 接口 | 用途 |
| --- | --- |
| `/healthz` | 检查前端进程是否存活 |
| `/readyz` | 检查前端是否可以接收请求 |
| `/statusz` | 查看服务和缓存索引状态 |
| `/metrics` | 获取 Prometheus 指标 |

运维接口的访问范围由集群网络策略控制；网关模式对外提供 `/v1`、`/tokenize` 和 `/detokenize` 客户端路径。

修改服务配置后，用 `foretoken deploy` 重新应用；用 `foretoken status` 查看状态，用 `foretoken delete` 删除部署。这些命令均传入同一份配置目录。
