<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

[简体中文](README_zh.md)

`foretoken-frontend` is the northbound Rust data plane behind the platform-owned
Gateway. It reuses the the local vLLM build source Rust request processors and incremental output
APIs while Foretoken owns multi-model routing and backend selection.

## Data flow

The control plane projects one versioned serving snapshot into the frontend Pod.
The frontend prepares a complete candidate generation and publishes it atomically
only after its model runtimes and backends are ready. Invalid or unready updates
leave the active generation unchanged.

```text
ModelService / ModelPool / ModelGroup
  → FrontendService controller
  → mounted serving.json serving snapshot
  → BackendRegistry + KvIndexer + per-model vLLM processors
  → RuntimeState { models, PipelineRouter, LlmFacadeResolver }
  → atomic active-generation swap
```

Each request loads one immutable active generation. vLLM owns request preparation
and output processing; Foretoken routes the generation request without
introducing a second text-generation path.

```text
OpenAI HTTP request
  → select pinned model runtime
  → vLLM text/chat processor → vllm_llm::GenerateRequest
  → PipelineRouter: node-list Filter → Scorer → Picker
  → BackendRegistry resolves the selected Aggregate or Prefill node
  → after Prefill, PipelineRouter independently selects one Decode node
  → LlmFacade → GenerateInput JSON
  → Group-local model-server → vLLM Llm / EngineCore
  → TokenEvent<TokenOutput> NDJSON
  → LlmFacade reconstructs the vllm_llm::GenerateOutput stream
  → vLLM text/chat output processor
  → SSE chunks or one collected HTTP response
```

Aggregate requests use one backend. For P/D requests, Router first selects and executes one Prefill node, then reads a fresh backend snapshot and selects one Decode node. P and D are not paired or reserved together. Only the Decode stream is exposed to the caller, and dropping it aborts unfinished backend work.

## Crate ownership and composition

`cmd` is the composition root. It validates one mounted serving snapshot into a
`BackendRegistryBuild`, constructs `BackendRegistry` and `KvIndexer` separately,
and installs a `PipelineRouter` with the selected pipeline, KV-prefix reader, and backend-statistics reader. Its runtime control refreshes backend readiness, load telemetry, and KV event deltas. Backend readiness does not depend on a capacity-admission snapshot.

```text
serving snapshot (mounted as serving.json)
  → BackendRegistryBuild
      ├→ BackendRegistry: RouteInventory + LlmFacadeResolver + route health
      └→ KvRuntimeConfig → KvIndexer: event sources, bindings, cursors, digest index
                                      └→ KvPrefixIndexer
BackendRegistry + KvIndexer → PipelineRouter
```

The dependency direction is one-way: `router → kv-indexer`; `backend-registry` derives
KV configuration but owns no KV runtime state; the Frontend composition root joins both owners. KV event access,
credentials, and index failures are opportunistic: they produce a neutral score and never
make an otherwise healthy route ineligible. For P/D routes, the binding always scores the
prefill source; decode cache state is not used as a prefix signal.

## Runtime contract

The controller provides:

- `FORETOKEN_LISTEN_ADDRESS`, for example `0.0.0.0:8080`;
- `FORETOKEN_SERVING_SNAPSHOT`, the mounted `serving.json` path;
- `FORETOKEN_STREAM_IDLE_SECONDS`, a positive per-token idle deadline;
- `FORETOKEN_KV_INDEX_KEY_PATH`, the optional mounted digest key used by the embedded `KvIndexer`;
- a writable `HF_HOME` cache used to resolve each serving snapshot's pinned
  tokenizer identity and revision.

On initial startup, the process remains live but not ready until it can prepare a
valid serving snapshot with healthy model-server backends. After a generation is
active, an invalid, unreadable, or unready update is retried without replacing it;
readiness continues to follow the active generation's backend health.

A serving snapshot may contain multiple public request models. Groups for the same `model`
must share one pinned model revision, tokenizer, and tokenizer revision; different
models may use different identities. The frontend loads one runtime bundle per model
and selects it before chat rendering, text lowering, routing, and decoding. Backend health and load telemetry are tracked per physical component. The Registry owns the node inventory and resolves the selected node through `LlmFacadeResolver`. The KV Indexer is best-effort and provides local and offloaded prefix signals. Router filters and scores the complete node list, then selects one Aggregate or Prefill node. After Prefill completes, Router reads a fresh snapshot and independently selects one Decode node. P/D execution is sequential: the internal prefill NDJSON stream is fully consumed to a normal terminal event before decode is submitted, and only decode output reaches the caller. P/D-alpha has mock coverage only:
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
serve multimodal requests, while P/D snapshots reject multimodal capabilities.

## Kubernetes runtime contract

Foretoken packages this component as an OCI image built from the repository root because the Rust workspace directly reuses the local vLLM build source under `third_party/vllm-rust/source/rust`.

The control plane creates and manages the frontend Deployment, mounts the serving snapshot and tokenizer cache, and exposes it only through the platform-managed Gateway. Users do not start frontend replicas individually.

The runtime image uses numeric user `65532`. OCI image startup with a real model-server and accelerator-backed EngineCore still requires Kubernetes integration validation; the current automated coverage uses mock tokenizers and model-server endpoints.

## Development build

Use a local vLLM Git checkout as the build source:

```bash
FORETOKEN_VLLM_SOURCE=/path/to/vllm make build-frontend
FORETOKEN_VLLM_SOURCE=/path/to/vllm make verify-frontend
FORETOKEN_VLLM_SOURCE=/path/to/vllm make image-frontend
```

The source selector validates the checkout and updates the ignored `third_party/vllm-rust/source` link. It does not modify the checkout.
