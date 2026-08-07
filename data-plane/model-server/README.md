<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Model Server

`foretoken-model-server` is the group-local Rust process for one model Pod. It
uses pinned vLLM Rust crates directly: `vllm-managed-engine` starts the
headless Python EngineCore, `vllm-engine-core-client` owns the handshake and
transport, `vllm-llm` provides generate/abort, and `vllm-tracing` initializes
process logging. Foretoken does not provide a Python launcher or process
supervisor.

The ModelGroup controller is the sole runtime-configuration owner. It supplies
one required, versioned JSON value in `FORETOKEN_VLLM_LAUNCH_PLAN` and the
required `FORETOKEN_INTERNAL_LISTEN`; container arguments are empty. The plan
contains immutable artifacts, TP/PP/DP/PCP/DCP and optional expert settings,
a tagged KV variant, lifecycle seconds, and allowlisted extra arguments. Rust
decodes it with unknown fields rejected, validates topology/lifecycle/KV and
extra arguments again, and is the sole renderer of vLLM argv. The managed
engine remains the sole renderer of positional model, `--headless`, loopback
handshake host/port, and data-parallel wiring; the launch renderer supplies
revision/tokenizer/topology/KV/one shutdown timeout/extra arguments.

Each `extraArgs` entry is one long-form option. The allowlist is
`--max-model-len`, `--dtype`, `--quantization`, `--gpu-memory-utilization`,
`--max-num-seqs`, `--max-num-batched-tokens`, `--enforce-eager`, and
`--disable-log-stats`. Typed artifacts, topology, expert, KV, lifecycle,
managed-engine options, aliases, duplicates, and `--config` fail closed.

The managed-engine upstream implementation places its loopback handshake
address in its command line and controlled lifecycle log; it is not a
credential. Foretoken accepts that upstream behavior and does not duplicate a
supervisor merely to hide it.

## Internal API

The listener is controller-owned. `/healthz` is available only while both the
managed Python process and EngineCore client are healthy. `/readyz`, generate,
and abort additionally require the server to be accepting work. During drain,
new work receives `503`; KV index availability does not alter EngineCore
readiness.

- `POST /v1/internal/generate` accepts tokenized vLLM inputs and returns NDJSON
  token events.
- `POST /v1/internal/abort` accepts a non-empty request-id list.
- `GET /v1/internal/telemetry` returns only version `1`, admission state, local
  accepted-and-not-terminal request count, and capacity. Capacity is the checked
  sum of the connected EngineCore `ready_responses().max_num_seqs`; it has no
  configured fallback and exposes no engine, request, KV, byte, or latency data.
- `GET /v1/internal/kv-index/delta` returns cursor-based opaque KV-prefix
  events.

The in-flight counter increments only after vLLM accepts a request. Its stream
ownership guard releases exactly once on terminal output, stream error/end, or a
client/body drop. It is the precise minimum local signal available because this
process is the only EngineCore client; it is not a scheduler-load, KV-byte, or
TTFT measurement.

On SIGTERM/Ctrl-C, client transport failure, managed child exit, or HTTP server
failure, the server stops admission, performs Axum graceful drain for the
configured bounded deadline, shuts down the vLLM client, then delegates
SIGTERM → timeout → SIGKILL and process-group reaping to
`ManagedEngineHandle::shutdown`. Kubernetes termination grace is the drain
ceiling plus five seconds for vLLM's upstream minimum engine shutdown window.

## Kubernetes runtime artifact

Build the Dockerfile from the repository root and pass `VLLM_RUNTIME_IMAGE`.
That artifact must contain `python3` and `vllm.entrypoints.cli.main`, built from
the same pinned vLLM commit
`5b14019576475224d86044b262e28a04a85d4086`. There is no verified matching
public image and CI intentionally does not build or claim compatibility with an
arbitrary runtime image. The selected artifact must be validated in Kubernetes
with its intended GPU profile before deployment.

## Development build

From the repository root, materialize the pinned vLLM Rust source before any
Cargo command or OCI build:

```bash
./scripts/bootstrap-vllm-rust.sh
cargo check --manifest-path data-plane/model-server/Cargo.toml --locked
docker build \
  --build-arg VLLM_RUNTIME_IMAGE=<validated-runtime-image> \
  -f data-plane/model-server/Dockerfile -t foretoken-model-server:dev .
```

`third_party/vllm-rust/source.lock.toml` pins the official upstream commit and
the checksum of the tracked patch. The bootstrap is idempotent; it fails rather
than reset or overwrite unexpected local changes under the ignored
`reference/upstream/vllm` worktree.
