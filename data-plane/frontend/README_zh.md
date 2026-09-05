<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

`foretoken-frontend` 接收推理请求并返回 OpenAI 兼容响应。Foretoken 控制器负责创建和配置 `FrontendService`；用户通过维护中的示例或自己的服务配置部署前端，而不需要单独启动进程。

默认模式通过 `LoadBalancer` 类型的 Kubernetes `Service` 直接暴露前端；网关模式通过绑定平台 Gateway 的 `HTTPRoute` 暴露。Gateway 负责域名、TLS、认证和其他入口策略。网关路由只暴露 `/v1`、`/tokenize` 和 `/detokenize`，不暴露运维接口；默认模式下，运维接口是否可从集群外访问取决于 LoadBalancer 和集群网络策略。

## 使用方式

按照仓库[快速开始](../../README_zh.md)部署前端并发送请求。一个前端可以提供多个公开模型，客户端通过请求中的 `model` 选择模型；目标模型暂时不可用时，前端不会静默改为其他模型。

前端支持普通 JSON 和 SSE 流式响应、Completion、Chat Completion、分词、工具调用、reasoning、structured output 与受能力约束的图片输入。图片输入当前只接受大小受限的 base64 `data:` 内容，不接受远程媒体 URL。

控制器负责配置并验证聚合部署、Prefill/Decode 分离和 Encoder/Prefill/Decode 分离拓扑。前端在控制器发布的拓扑内选择各执行阶段。当前分离式推理需要控制器接受的运行 profile 和传输方式，不能通过前端请求参数配置。

## 接口访问范围

| 范围 | 接口 | 用途 |
| --- | --- | --- |
| 客户端 | `/v1/*`、`/tokenize`、`/detokenize` | 发送推理请求和发现已配置模型 |
| 平台运维 | `/healthz`、`/readyz`、`/statusz`、`/metrics` | 探针、运行状态诊断和 Prometheus 抓取 |
| 控制器内部 | `/internal/autoscaling/telemetry` | 控制器收集遥测；不是客户端契约 |

`/v1/models` 返回当前已发布运行代中的模型。模型出现在列表中不保证后续请求时其后端仍然健康；客户端需要处理不可用响应。

`/healthz` 表示前端进程正在运行。`/readyz` 表示已有已发布的运行代可以接收新请求，但不保证每个已配置模型都有健康后端路径。`/statusz` 为平台运维者提供运行代和 KV 索引诊断信息。`/metrics` 是 Prometheus 抓取端点。

模型或路由变化时，前端只会在新运行代成功发布后启用它。无效或未就绪的更新不会替换当前运行代，已经接收的请求会在其所属运行代中继续完成。
