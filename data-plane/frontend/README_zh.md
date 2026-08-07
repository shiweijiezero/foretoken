<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

`foretoken-frontend` 是位于平台 Envoy 网关之后、直接处理推理请求的 Rust 数据面。它尽可能复用 vLLM Rust 已有的请求处理器和增量输出 API，由 Foretoken 补充多模型路由、后端选择与 P/D 编排能力。

## 数据链路

控制面持续监听 ModelService、ModelPool 和 ModelGroup，并把当前可用的模型及其后端地址写入 `serving.json`。Frontend 发现文件变化后，会加载新模型对应的 tokenizer 和请求处理器，并检查 model-server 是否就绪；全部成功后才启用新配置，否则继续使用上一份可用配置。

模型监听侧：
```text
control-plane/controllers/
  ModelService / ModelPool / ModelGroup → serving.json
        ↓
data-plane/frontend/src/cmd/
  监听 serving.json，组合并启用新的 Frontend 配置
        ↓
data-plane/frontend/src/backend-registry/
  构建后端列表，维护 health、metadata 和 capacity
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
  按 capability、health、capacity 过滤，再按 KV locality 和 load 选择后端
        ↓
data-plane/frontend/src/backend-registry/
  根据 Router 的选择找到对应的后端连接
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

Aggregate 请求只使用一个后端。P/D 请求会同时预留一个 Prefill component 和一个 Decode component；Prefill 正常完成后才会提交 Decode，并且只把 Decode stream 返回给调用方。请求结束或 stream 被提前丢弃时，Frontend 会释放 reservation，并中止尚未完成的后端任务。

## Crate 职责与组合

`cmd` 是整个 Frontend 的 composition root。它负责读取和校验 serving snapshot，构建 `BackendRegistry`、`KvIndexer` 和每个模型的 processor，最后把它们组合成 `PolicyRouter` 与完整的 `RuntimeState`。

```text
data-plane/frontend/src/cmd/               进程入口，读取 serving.json 并组合各层
data-plane/frontend/src/server/            HTTP 接口、模型运行状态与响应输出
data-plane/frontend/src/chat/              Chat message、工具调用和 reasoning 请求转换
data-plane/frontend/src/text/              文本请求转换与增量解码
data-plane/frontend/src/tokenizer/         文本与 token ID 转换
data-plane/frontend/src/parser/            工具调用和 reasoning 结果解析
data-plane/frontend/src/router/            后端过滤、排序、P/D 选择与容量预留
data-plane/frontend/src/backend-registry/  后端目录、运行状态与连接信息
data-plane/frontend/src/kv-indexer/        KV prefix locality 索引
data-plane/frontend/src/llm-facade/        model-server HTTP 调用与 P/D 执行顺序
data-plane/model-protocol/                 Frontend 与 model-server 的通信格式
data-plane/model-server/                   Group 内的模型推理服务
data-plane/frontend/src/metrics/           指标采集与 HTTP middleware
data-plane/frontend/src/tracing/           tracing 初始化
```

`router` 定义 KV locality 评分接口，`kv-indexer` 负责实现。Router 只使用评分结果，不关心 KV 索引如何构建；两者由 `cmd` 组合。
对于 P/D route，KV locality 只用于选择 Prefill component，不使用 Decode 侧的 cache state。

## 自定义实现

Frontend 已预留代码级扩展接口：

- 实现 `Router`，可以替换后端过滤、排序和选择策略；
- 实现 `KvPrefixScorer`，可以替换 KV locality 评分方式；
- 实现 `LlmFacade` 和 `LlmFacadeResolver`，可以接入其他后端协议；
- 实现 `Generation`，可以替换 HTTP 层以下的完整生成链路。

这些接口由 `data-plane/frontend/src/cmd/` 统一组装。当前版本尚未提供通过 CRD、配置文件或动态插件加载自定义实现的能力；用户需要在 Rust 代码中实现相应接口，并构建自己的 Frontend OCI image。

## 运行约定

Controller 会向 Frontend 提供：

- `FORETOKEN_LISTEN_ADDRESS`：监听地址，例如 `0.0.0.0:8080`；
- `FORETOKEN_SERVING_SNAPSHOT`：挂载的 `serving.json` 路径；
- `FORETOKEN_STREAM_IDLE_SECONDS`：相邻 token 之间允许的最长空闲时间，必须为正数；
- `FORETOKEN_KV_INDEX_KEY_PATH`：`KvIndexer` 使用的可选 digest key 路径；
- 可写的 `HF_HOME` cache：用于加载 serving snapshot 固定的 tokenizer identity 和 revision。

初次启动时，进程会保持存活，但只有在 serving snapshot 有效、模型 processor 准备完成且存在健康后端后才会 Ready。当前运行状态发布后，如果新的 snapshot 无效、暂时不可读或后端尚未就绪，Frontend 会保留现有状态并继续重试，不会主动中断已有服务。

一份 serving snapshot 可以包含多个对外提供的模型。同一 `model` 下的所有 Group 必须使用相同的 model revision、tokenizer 和 tokenizer revision；不同模型可以使用各自独立的版本。Frontend 会为每个模型加载一套固定的 runtime bundle，并在 chat rendering、text lowering、routing 和 decoding 之前选定对应 bundle。

当前支持以下接口：

- `POST /v1/completions`；
- `POST /v1/chat/completions`，支持文本、工具调用、reasoning、structured output，以及策略允许的 multimodal message；
- `POST /v1/generate`，作为简化的 completion alias；
- `POST /tokenize` 和 `POST /detokenize`；
- `GET /v1/models`、`GET /v1/models/{model}`、`/healthz`、`/readyz` 和 `/metrics`。

流式与非流式响应共用同一条增量解码路径。
Multimodal HTTP input 目前只接受有大小限制的 base64 `data:` 内容；

## Kubernetes 运行约定

Foretoken 将 Frontend 打包为 OCI image。由于 Rust workspace 直接复用 `reference/upstream/vllm/rust` 中固定版本的上游源码，镜像需要从仓库根目录构建。

控制面负责创建和管理 Frontend Deployment，挂载 serving snapshot 与 tokenizer cache，并通过平台 Envoy 网关对外提供服务。用户不需要单独创建或启动 Frontend 副本。

## 开发构建

执行 Cargo 命令或构建 OCI image 前，先在仓库根目录准备固定版本的上游 Rust 源码：

```bash
./scripts/bootstrap-vllm-rust.sh
cargo check --manifest-path data-plane/frontend/Cargo.toml --workspace --locked
docker build -f data-plane/frontend/Dockerfile -t foretoken-frontend:dev .
```

Bootstrap 脚本会读取 `third_party/vllm-rust/source.lock.toml`，校验 tracked patch checksum，再把固定 patch 应用到 Git 忽略的上游源码 worktree。
