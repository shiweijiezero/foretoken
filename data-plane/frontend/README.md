<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Frontend

`foretoken-frontend` is the northbound Rust data plane behind the platform-owned
Gateway. It reuses the pinned vLLM Rust request processors and incremental output
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
  → RuntimeState { models, PolicyRouter, LlmFacadeResolver }
  → atomic active-generation swap
```

Each request loads one immutable active generation. vLLM owns request preparation
and output processing; Foretoken routes the canonical generation request without
introducing a second text-generation path.

```text
OpenAI HTTP request
  → select pinned model runtime
  → vLLM text/chat processor → canonical vllm_llm::GenerateRequest
  → PolicyRouter: capability/health/capacity filter → KV/load ranking
  → BackendRegistry resolves aggregate backend or same-domain P/D pair
  → LlmFacade → GenerateInput JSON
  → Group-local model-server → vLLM Llm / EngineCore
  → TokenEvent<TokenOutput> NDJSON
  → LlmFacade reconstructs canonical vllm_llm::GenerateOutput stream
  → vLLM text/chat output processor
  → SSE chunks or one collected HTTP response
```

Aggregate requests use one backend. P/D requests reserve a prefill and decode
component atomically, fully consume prefill, then expose only the decode stream to
the caller. Dropping the canonical stream releases the reservation and aborts
unfinished backend work.

## Crate ownership and composition

`cmd` is the composition root. It validates one mounted serving snapshot into a
`BackendRegistryBuild`, constructs `BackendRegistry` and `KvIndexer` separately,
and installs `PolicyRouter::new(registry, kv_indexer)`. Its runtime control refreshes
backend readiness, capacity telemetry, and KV event deltas together; both registry health
and a valid capacity snapshot affect readiness.

```text
serving snapshot (mounted as serving.json)
  → BackendRegistryBuild
      ├→ BackendRegistry: RouteInventory + LlmFacadeResolver + route health
      └→ KvRuntimeConfig → KvIndexer: event sources, bindings, cursors, digest index
                                      └→ KvPrefixScorer
BackendRegistry + KvIndexer → PolicyRouter
```

The dependency direction is one-way: `kv-indexer → router`; `backend-registry` derives
KV configuration but owns no KV runtime state; `cmd` joins both owners. KV event access,
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
and selects it before chat rendering, text lowering, routing, and decoding. Backend health
and capacity telemetry are tracked per component. The Registry owns immutable component/domain
inventory and resolves an aggregate target or selected dynamic P/D pair through one `VllmFacade`.
The KV Indexer is best-effort and only scores prefill components. The Router filters model,
revision, capability, component health, and capacity; ranks P by prefix locality then load; and
selects a same-domain D by load. It atomically reserves the final P+D pair before prefill begins,
holding it until the decode stream terminates or is dropped. A failed component removes
only that component. P/D execution is sequential: the internal prefill NDJSON stream is fully
consumed to a normal terminal event before decode is submitted, and only decode output reaches
the caller. Reservations are
process-local and do not provide cross-replica consistency. P/D-alpha has mock coverage only:
real GPU/RDMA Mooncake connector timing still requires conformance validation, and this document
makes no GPU/RDMA conformance claim. The frontend remains
ready while any route is healthy; a model with no healthy route is omitted from
`/v1/models` and only that model's requests fail. There is no cross-model fallback. The
supported requests are:

- `POST /v1/completions`;
- `POST /v1/chat/completions` with text, tools, reasoning, structured-output, and
  policy-enabled multimodal messages;
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

Foretoken packages this component as an OCI image built from the repository root because the Rust workspace directly reuses the pinned vLLM sources under `reference/upstream/vllm/rust`.

The control plane creates and manages the frontend Deployment, mounts the serving snapshot and tokenizer cache, and exposes it only through the platform-managed Gateway. Users do not start frontend replicas individually.

The runtime image uses numeric user `65532`. OCI image startup with a real model-server and accelerator-backed EngineCore still requires Kubernetes integration validation; the current automated coverage uses mock tokenizers and model-server endpoints.

## Development build

From the repository root, materialize the pinned vLLM Rust source before any
Cargo command or OCI build:

```bash
./scripts/bootstrap-vllm-rust.sh
cargo check --manifest-path data-plane/frontend/Cargo.toml --workspace --locked
docker build -f data-plane/frontend/Dockerfile -t foretoken-frontend:dev .
```

The bootstrap reads `third_party/vllm-rust/source.lock.toml`, verifies the
tracked patch checksum, and applies the pinned patch to the ignored
`reference/upstream/vllm` worktree. It is idempotent and refuses to reset or
overwrite unexpected local changes there.
