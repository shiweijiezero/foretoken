<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 分布式追踪

[可观测性](README_zh.md) | [English](tracing.md)

Foretoken 的 frontend 和 model-server 接收 W3C `traceparent`/`tracestate`，以 JSON 请求日志输出 `request_id`、`trace_id` 和 `span_id`，并在配置后通过 OTLP/HTTP 导出采样 span。未配置 endpoint 时不会导出追踪数据。

## 配置平台

使用维护的 platform values 示例，不要直接编辑 Chart：

```bash
foretoken install --values examples/tracing/platform-values.yaml
```

将示例中的地址替换为实际 Collector：

```yaml
observability:
  tracing:
    endpoint: http://otel-collector.observability.svc:4318
    samplingRatio: 0.1
    # headersSecret:
    #   name: otel-exporter-headers
    #   key: headers
```

`endpoint` 是 OTLP/HTTP 基础地址，Foretoken 使用其 `/v1/traces` 路径。`samplingRatio` 是基于父 span 的采样概率。可选的 `headersSecret` 从 Kubernetes Secret 提供完整的 OTLP headers 值；凭据不要写入 YAML 或 Git。

平台会将以下环境变量传入 frontend 和 model-server Pod：

- `OTEL_EXPORTER_OTLP_ENDPOINT`；
- `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`；
- `OTEL_TRACES_SAMPLER=parentbased_traceidratio`；
- `OTEL_TRACES_SAMPLER_ARG=<samplingRatio>`；
- 可选的、来自 Secret 的 `OTEL_EXPORTER_OTLP_HEADERS`。

设置 `OTEL_TRACES_EXPORTER=none` 或删除 endpoint 可关闭导出。没有 exporter 时，trace context 传播和 request ID 仍可安全使用。

## 跟踪一次请求

客户端可以提供 `traceparent`；Foretoken 创建 server span 并返回服务端拥有的 `x-request-id`。frontend 将 W3C context 传给 model-server；选定的 backend 支持追踪时，model-server 再传给 EngineCore。日志只包含有界的标识和操作元数据，不记录 prompt、token 或凭据。

流式 span 会一直保持到响应 body 完成、失败或取消。SSE error event 会单独记录为推理失败；HTTP 响应已经开始后，即使状态是 `2xx` 也不代表推理成功。

## Collector 要求

请在配置的地址部署 OpenTelemetry Collector 或其他 OTLP/HTTP receiver，并由平台负责保留周期、访问控制、批处理以及导出到 trace backend。Foretoken 不安装 Collector、追踪数据库或日志后端。

性能剖析和告警通知是独立运维能力，分别参阅[告警](alerting_zh.md)、[Lark 集成](integrations/lark/README_zh.md)和[性能剖析](profiling_zh.md)。
