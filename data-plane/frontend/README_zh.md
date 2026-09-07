<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

`foretoken-frontend` 接收推理请求并返回 OpenAI 兼容响应。通过维护中的示例或自己的服务配置声明 `FrontendService`，Foretoken 会自动创建并配置前端工作负载。

## 使用方式

按照仓库[快速开始](../../README_zh.md)部署前端并发送请求。一个前端可以提供多个公开模型，客户端通过请求中的 `model` 选择模型；目标模型暂时不可用时，前端不会静默改为其他模型。

前端支持普通 JSON 和 SSE 流式响应、Completion、Chat Completion、分词、工具调用、reasoning、structured output 与受能力约束的图片输入。图片输入当前只接受大小受限的 base64 `data:` 内容，不接受远程媒体 URL。

通过 `ModelService` 配置聚合部署、预填充/解码分离（P/D）或编码/预填充/解码分离（E/P/D）。分离式推理需要平台支持所选运行时和传输方式。

## 接口访问范围

默认模式通过 `LoadBalancer` 类型的 Kubernetes `Service` 暴露前端。网关模式通过绑定平台 Gateway 的 `HTTPRoute` 暴露 `/v1`、`/tokenize` 和 `/detokenize`。域名解析、TLS、认证和其他入口策略需在 Gateway 部署中配置。默认模式下，运维接口的访问范围取决于 LoadBalancer 和集群网络策略。

| 范围 | 接口 | 用途 |
| --- | --- | --- |
| 客户端 | `/v1/*`、`/tokenize`、`/detokenize` | 发送推理请求和发现已配置模型 |
| 平台运维 | `/healthz`、`/readyz`、`/statusz`、`/metrics` | 探针、运行状态诊断和 Prometheus 抓取 |

`/v1/models` 返回前端当前生效配置中的模型。

`/healthz` 表示前端进程正在运行。`/readyz` 表示服务配置已生效，前端可以接收新请求，但不保证每个已配置模型都有健康后端路径。`/statusz` 为平台运维者提供运行状态和 KV 索引诊断信息。`/metrics` 是 Prometheus 抓取端点。

模型配置准备完成后，会在运行中的前端进程内更新；已经开始执行的请求保留其选定的配置。修改 `FrontendService.spec.routerPipeline` 则通过前端 Deployment 滚动更新生效。
