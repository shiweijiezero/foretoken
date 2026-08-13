<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

[English](README.md)

`foretoken-frontend` 是位于平台托管、兼容 Gateway API 的 Gateway 之后、直接处理推理请求的 Rust 数据面。它尽可能复用 vLLM Rust 已有的请求处理器和增量输出 API，由 Foretoken 补充多模型路由、模型服务器选择与 P/D 编排能力。

## 数据链路

控制面持续监听 ModelService、ModelPool 和 ModelGroup，并把当前可用的模型及其模型服务器地址写入路由配置 `serving.json`。Frontend 发现文件变化后，会加载新模型对应的 tokenizer 和请求处理器，并检查 model-server 是否就绪；全部成功后才启用新配置，否则继续使用上一份可用配置。

模型监听侧：
```text
control-plane/controllers/
  ModelService / ModelPool / ModelGroup → serving.json
        ↓
data-plane/frontend/src/cmd/
  监听 serving.json，组合并启用新的 Frontend 配置
        ↓
data-plane/frontend/src/backend-registry/
  构建模型服务器列表，维护 health、metadata 和 capacity
        ↓
data-plane/frontend/src/kv-indexer/
  构建可选的 KV prefix locality 索引
        ↓
data-plane/frontend/src/server/
  原子启用新的 RuntimeState
```

用户请求侧：
```text
data-plane/frontend/src/server/
  接收 OpenAI HTTP 请求，并根据 model 选择对应的请求处理器
        ↓
data-plane/frontend/src/chat/
  Chat 请求：把 messages、tools 和 reasoning 配置转换为 TextRequest
        ↓
data-plane/frontend/src/text/
  Completion/Chat：把文本请求转换为统一的生成请求
        ↕
data-plane/frontend/src/tokenizer/
  负责文本与 token ID 之间的转换
        ↓
data-plane/frontend/src/router/
  按 model、revision、capability 和 health 构造候选集，再按 KV locality 和 load 选择模型服务器
        ↓
data-plane/frontend/src/backend-registry/
  根据 Router 的选择找到对应的模型服务器连接
        ↓
data-plane/frontend/src/llm-facade/
  转换为 GenerateInput JSON，并执行 aggregate 或 P/D 请求
        ↓
data-plane/model-protocol/
  定义 GenerateInput 与 TokenEvent<TokenOutput> NDJSON wire contract
        ↓
data-plane/model-server/
  执行模型推理，返回 NDJSON token stream
        ↓
data-plane/frontend/src/llm-facade/
  把 NDJSON 恢复为统一的 token stream
        ↓
data-plane/frontend/src/server/
  处理生成结果，返回 SSE 流式响应或聚合 JSON 响应
```

Aggregate 请求只使用一个模型服务器。P/D 请求先选择并执行一个 Prefill 模型服务器，完成后重新读取最新路由配置，再选择一个 Decode 模型服务器。Frontend 只把 Decode stream 返回给调用方，stream 被提前丢弃时会中止尚未完成的模型服务器任务。

## 组件职责

`cmd` 是整个 Frontend 的进程入口和组件组装点。它负责读取和校验路由配置（`serving.json`），构建 `BackendRegistry`、`KvIndexer` 和每个模型的 processor，最后把它们组装成 `PipelineRouter` 与完整的 `RuntimeState`。

```text
data-plane/frontend/src/cmd/               进程入口，读取 serving.json 并组合各层
data-plane/frontend/src/server/            HTTP 接口、模型运行状态与响应输出
data-plane/frontend/src/chat/              Chat message、工具调用和 reasoning 请求转换
data-plane/frontend/src/text/              文本请求转换与增量解码
data-plane/frontend/src/tokenizer/         文本与 token ID 转换
data-plane/frontend/src/parser/            工具调用和 reasoning 结果解析
data-plane/frontend/src/router/            模型服务器节点过滤、评分与分阶段选择
data-plane/frontend/src/backend-registry/  模型服务器目录、运行状态与连接信息
data-plane/frontend/src/kv-indexer/        KV prefix locality 索引
data-plane/frontend/src/llm-facade/        model-server HTTP 调用与 P/D 执行顺序
data-plane/model-protocol/                 Frontend 与 model-server 的通信格式
data-plane/model-server/                   Group 内的模型服务器
data-plane/frontend/src/metrics/           指标采集与 HTTP middleware
data-plane/frontend/src/tracing/           tracing 初始化
```

`kv-indexer` 定义并实现 KV prefix 查询接口，Router 只消费查询结果，不关心索引如何构建；两者由 Frontend 组件组装点组合。
对于 P/D route，KV locality 只用于选择 Prefill component，不使用 Decode 侧的 cache state。

## 自定义实现

Frontend 已预留代码级扩展接口：

- 实现 `Router`，可以替换模型服务器过滤、排序和选择策略；
- 实现 `KvPrefixIndexer`，可以替换 local/offloaded prefix 查询方式；
- 实现 `LlmFacade` 和 `LlmFacadeResolver`，可以接入其他模型服务器协议；
- 实现 `Generation`，可以替换 HTTP 层以下的完整生成链路。

这些接口由 `data-plane/frontend/src/cmd/` 统一组装。用户自定义需要在 Rust 代码中实现相应接口，并构建自己的 Frontend OCI image。

## 运行

Controller 会向 Frontend 提供：

- `FORETOKEN_LISTEN_ADDRESS`：监听地址，例如 `0.0.0.0:8080`；
- `FORETOKEN_SERVING_SNAPSHOT`：挂载的 `serving.json` 路径；
- `FORETOKEN_STREAM_IDLE_SECONDS`：相邻 token 之间允许的最长空闲时间，必须为正数；
- `FORETOKEN_KV_INDEX_KEY_PATH`：`KvIndexer` 使用的可选 digest key 路径；
- 可写的 `HF_HOME` cache：用于加载路由配置固定的 tokenizer identity 和 revision。

初次启动时，进程会保持存活，但只有在路由配置有效、模型 processor 准备完成且存在健康模型服务器后才会 Ready。当前运行状态发布后，如果新的路由配置无效、暂时不可读或模型服务器尚未就绪，Frontend 会保留现有状态并继续重试，不会主动中断已有服务。

一份路由配置可以包含多个对外提供的模型。同一 `model` 下的所有 Group 必须使用相同的 model revision、tokenizer 和 tokenizer revision；不同模型可以使用各自独立的版本。Frontend 会为每个模型加载一套固定的 runtime bundle，并在 chat rendering、text lowering、routing 和 decoding 之前选定对应 bundle。

当前支持以下接口：

- `POST /v1/completions`；
- `POST /v1/chat/completions`，支持文本、工具调用、reasoning、structured output，以及策略允许的 multimodal message；
- `POST /v1/generate`，作为简化的 completion alias；
- `POST /tokenize` 和 `POST /detokenize`；
- `GET /v1/models`、`GET /v1/models/{model}`、`/healthz`、`/readyz` 和 `/metrics`。

流式与非流式响应共用同一条增量解码路径。
Multimodal HTTP input 目前只接受有大小限制的 base64 `data:` 内容。
