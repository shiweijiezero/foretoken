<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

[English](README.md)

`foretoken-frontend` 是位于网关层之后、直接处理推理请求的 Rust 数据面。它为客户端提供统一的生成接口，并在多个模型和推理实例之间完成请求路由、流式响应与运行状态管理。

## 主要能力

- 提供 OpenAI 兼容的 Completion 和 Chat Completion API；
- 在同一个 frontend 中对外提供多个模型；
- 根据模型、请求能力、实例健康状态、负载和 KV cache 复用机会选择推理实例；
- 支持聚合部署、Prefill/Decode 分离和 Encoder/Prefill/Decode 分离；
- 同时支持 SSE 流式响应和普通 JSON 响应；
- 支持工具调用、reasoning、structured output 和受能力约束的图片输入；
- 在路由配置更新或实例变化时继续完成已经接收的请求；
- 提供健康检查、就绪状态和运行指标。

## 请求路径

```text
客户端
  ↓
LoadBalancer 或网关
  ↓
foretoken-frontend
  ├─ 校验并转换请求
  ├─ 选择模型和可用推理实例
  ├─ 执行聚合或分离式推理流程
  └─ 返回流式或非流式响应
  ↓
模型服务
```

客户端通过请求中的 `model` 选择模型。Frontend 只会把请求发送到满足该模型能力并且当前可用的实例；一个模型暂时不可用时，不会影响其他健康模型继续服务，也不会自动把请求转发到不同模型。

当运行中的模型实例或路由发生变化时，Frontend 会在新配置准备完成后再启用它。无效或尚未就绪的更新不会替换当前可用配置，也不会主动中断正在进行的请求。

## 访问方式

### 本地模式

本地模式通过 `LoadBalancer` Service 提供访问地址。具体访问命令见仓库根目录的 Quick Start。

### 网关模式

安装 Gateway Controller 后，Foretoken Chart 可以创建专用的 `GatewayClass` 和 `Gateway`，也可以复用平台已有网关。Foretoken 为前端服务创建 `HTTPRoute`，域名、TLS、认证和其他入口策略继续由平台网关管理。

## HTTP 接口

- `POST /v1/completions`
- `POST /v1/chat/completions`
- `POST /v1/generate`
- `POST /tokenize`
- `POST /detokenize`
- `GET /v1/models`
- `GET /v1/models/{model}`
- `GET /healthz`
- `GET /readyz`
- `GET /metrics`

流式和非流式请求使用相同的生成语义。图片输入目前接受有大小限制的 base64 `data:` 内容，不接受远程媒体 URL。

## 健康与就绪状态

- `/healthz` 表示 frontend 进程能够正常工作；
- `/readyz` 表示至少已有可用的模型路由，可以接收推理请求；
- `/v1/models` 只返回当前具有健康推理路径的模型。

Frontend 通常由 Foretoken Controller 创建和配置，用户不需要单独启动或维护它。
