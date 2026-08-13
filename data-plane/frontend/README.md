<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

[简体中文](README_zh.md)

`foretoken-frontend` is the northbound Rust data plane behind the platform-owned
Gateway. It reuses vLLM Rust request processors and incremental output APIs while Foretoken
owns multi-model routing and model-server selection.

## Data flow

The control plane projects one routing configuration into the frontend Pod.
The frontend prepares a complete pending routing configuration and publishes it atomically
only after its model runtimes and model servers are ready. Invalid or unready updates
leave the active routing configuration unchanged.

```text
ModelService / ModelPool / ModelGroup
  → FrontendService controller
  → mounted serving.json routing configuration
  → BackendRegistry + KvIndexer + per-model vLLM processors
  → RuntimeState { models, PipelineRouter, LlmFacadeResolver }
  → atomic active-routing-configuration swap
```

Each request loads one immutable active routing configuration. vLLM owns request preparation
and output processing; Foretoken routes the generation request without
introducing a second text-generation path.

```text
OpenAI HTTP request
  → select pinned model runtime
  → vLLM text/chat processor → vllm_llm::GenerateRequest
  → PipelineRouter: node-list Filter → Scorer → Picker
  → BackendRegistry resolves the selected Aggregate or Prefill model server
  → after Prefill, PipelineRouter independently selects one Decode node
  → LlmFacade → GenerateInput JSON
  → Group-local model server → vLLM Llm / EngineCore
  → TokenEvent<TokenOutput> NDJSON
  → LlmFacade reconstructs the vllm_llm::GenerateOutput stream
  → vLLM text/chat output processor
  → SSE chunks or one collected HTTP response
```

Aggregate requests use one model server. For P/D requests, Router first selects and executes one Prefill model server, then reads a fresh routing configuration and selects one Decode model server. P and D are not paired or reserved together. Only the Decode stream is exposed to the caller, and dropping it aborts unfinished model-server work.

## Component responsibilities

`cmd` is the process entry and component assembly point. It validates one mounted routing configuration into a
`BackendRegistryBuild`, constructs `BackendRegistry` and `KvIndexer` separately,
and installs a `PipelineRouter` with the selected pipeline, KV-prefix reader, and model-server statistics reader. Its runtime control refreshes model-server readiness, load telemetry, and KV event deltas. Model-server readiness does not depend on a capacity-admission snapshot.

```text
routing configuration (mounted as serving.json)
  → BackendRegistryBuild
      ├→ BackendRegistry: RouteInventory + LlmFacadeResolver + route health
      └→ KvRuntimeConfig → KvIndexer: event sources, bindings, cursors, digest index
                                      └→ KvPrefixIndexer
BackendRegistry + KvIndexer → PipelineRouter
```

The dependency direction is one-way: `router → kv-indexer`; `backend-registry` derives
KV configuration but owns no KV runtime state; the Frontend component assembly joins both owners. KV event access,
credentials, and index failures are opportunistic: they produce a neutral score and never
make an otherwise healthy route ineligible. For P/D routes, the binding always scores the
prefill source; decode cache state is not used as a prefix signal.

## Runtime requirements

The controller provides:

- `FORETOKEN_LISTEN_ADDRESS`, for example `0.0.0.0:8080`;
- `FORETOKEN_SERVING_SNAPSHOT`, the mounted `serving.json` path;
- `FORETOKEN_STREAM_IDLE_SECONDS`, a positive per-token idle deadline;
- `FORETOKEN_KV_INDEX_KEY_PATH`, the optional mounted digest key used by the embedded `KvIndexer`;
- a writable `HF_HOME` cache used to resolve each routing configuration's pinned
  tokenizer identity and revision.

On initial startup, the process remains live but not ready until it can prepare a
valid routing configuration with healthy model servers. After a routing configuration is
active, an invalid, unreadable, or unready update is retried without replacing it;
readiness continues to follow the active routing configuration's model-server health.

A routing configuration may contain multiple public request models. Groups for the same `model`
must share one pinned model revision, tokenizer, and tokenizer revision; different
models may use different identities. The frontend loads one runtime bundle per model
and selects it before chat rendering, text lowering, routing, and decoding. Model-server health and load telemetry are tracked per physical component. The Registry owns the node inventory and resolves the selected node through `LlmFacadeResolver`. The KV Indexer is best-effort and provides local and offloaded prefix signals. Router filters and scores the complete node list, then selects one Aggregate or Prefill node. After Prefill completes, Router reads a fresh routing configuration and independently selects one Decode model server. P/D execution is sequential: the internal prefill NDJSON stream is fully consumed to a normal terminal event before decode is submitted, and only decode output reaches the caller. P/D-alpha has mock coverage only:
real GPU/RDMA Mooncake connector timing still requires conformance validation, and this document
makes no GPU/RDMA conformance claim. The frontend remains
ready while any route is healthy; a model with no healthy route is omitted from
`/v1/models` and only that model's requests fail. There is no cross-model fallback. The
supported requests are:

- `POST /v1/completions`;
- `POST /v1/chat/completions` with text, tools, reasoning, structured-output, and
  capability-gated multimodal messages;
- `POST /v1/generate` as the simplified completion alias;
- `POST /tokenize` and `POST /detokenize`;
- `GET /v1/models`, `GET /v1/models/{model}`, `/healthz`, `/readyz`, and `/metrics`.

Streaming and non-streaming responses share vLLM's incremental detokenization
path. Optional request features are capability-gated and never silently
downgraded. Multimodal HTTP input is currently fail-closed to bounded base64
`data:` content; remote media URLs are rejected until the pinned connector has
connection-time host/IP and redirect enforcement. Aggregate model runtimes may
serve multimodal requests, while P/D routing configurations reject multimodal capabilities.
