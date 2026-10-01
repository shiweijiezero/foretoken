<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Draft/Target services

English | [简体中文](README_zh.md)

Run independently selected Draft and Target models on separate GPUs or hosts.
The frontend selects a pair, coordinates proposal and verification rounds, and
streams only Target-confirmed output. Mooncake transfers the Draft's full proposal
distributions between GPU workers; candidate IDs and tensor descriptors use HTTP.
Each engine owns its own scheduler and KV cache.

Use the [Kubernetes example](../../examples/draft-target/README.md) for deployment
through the existing Controller and Router. The commands below start the role
services directly for use with the [Rust frontend example](docs/frontend-workflow.md).

For NVIDIA source builds, `make image-model-server` builds the pinned CUDA 13
engine and installs this plugin, including its vLLM subprocess entry point.
`foretoken install -e .` uses the same runtime by default. The host must support
CUDA 13; an explicit `INFERENCE_ENGINE_IMAGE` override must provide the compatible
engine and Mooncake environment described below. MetaX uses its separate runtime
build and the compatibility requirements below.

## Start two roles

Both hosts need native vLLM `0.30.1rc1.dev194+g3b4566c5c`, or the repository's
MetaX source pair (`vllm==0.30.0.dev0`, `vllm-metax==0.29.0.dev0`),
Python 3.10+, compatible PyTorch and Mooncake wheels, and working GPU/RDMA access.
Install FastAPI, Uvicorn, AnyIO, Pydantic 2 and HTTPX in that environment.
The package uses native class-selection interfaces and small process-local runtime
patches; it does not edit installed vLLM files or require a separate engine branch.
It does not install vLLM or register a `vllm serve` mode. The adapter is tied to
these engine versions; see the [integration contract](docs/mrv2-integration.md).

For MetaX, use the patched [source runtime](../../deploy/inference-engines/vllm-metax/),
including its FlashAttention sequence-length fix for Model Runner V2. Keep
`VLLM_USE_V2_MODEL_RUNNER=1` explicit: the platform defaults to the older runner.
Use a MACA-compatible Mooncake build for RDMA; the source image sets
`MC_MACA_HOST_TRANSPORT=1`. Successful generation does not establish GPU-direct
transfer, cross-host operation or a performance improvement. MetaX greedy output
can differ from Target-only decoding as batch shapes change.

On systems using the loaded `nvidia-peermem` kernel module, set
`WITH_NVIDIA_PEERMEM=1` in both process environments. Otherwise Mooncake's default
DMA-BUF path requires corresponding driver support. Containers also need GPU and
RDMA device access, the RDMA userspace libraries, and sufficient locked memory.

Choose model names or local paths for `DRAFT_MODEL` and `TARGET_MODEL`. Both
models must have the same vocabulary size and token-ID meanings. Set `TOKENIZER`
to their shared tokenizer, normally the Target tokenizer. Set `DRAFT_IP` and
`TARGET_IP` to each host's routable local address, without a port.

On each host, install the package from the repository root:

```bash
python -m pip install --no-deps ./data-plane/dt-plugin
```

On the Draft host:

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role draft \
  --model "$DRAFT_MODEL" --tokenizer "$TOKENIZER" \
  --host 0.0.0.0 --rdma-host "$DRAFT_IP"
```

On the Target host:

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role target \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --host 0.0.0.0 --rdma-host "$TARGET_IP"
curl http://127.0.0.1:19100/status
```

`/status` should report the expected model and role, `accepting: true`, and
`candidate_format: "token_ids_log_probs"`. Both selected roles must advertise the
same format. Start the frontend using their HTTP endpoints; the role services
are internal APIs and do not expose public OpenAI endpoints or authentication.

Each role uses one GPU worker, eager execution and synchronous local
scheduling. Requests waiting for remote proposals do not block other requests.
The role launcher includes `foretoken_dt` in an inherited `VLLM_PLUGINS` allowlist
so spawned engine processes load the required hooks. On MetaX it also includes
the platform's `metax` plugin.
The role accepts native vLLM model, memory and batching options.
`--draft-token-budget` sets the maximum candidates per round (default 3).
`--port` changes the HTTP port (default 19100); `--rdma-nic` optionally selects
an HCA. Without a NIC filter, Mooncake chooses among visible devices.
The HTTP and dynamically allocated Mooncake handshake ports must be reachable.

The role launcher defaults vLLM's native `VLLM_BATCH_INVARIANT` to `1` on NVIDIA
and `0` on MetaX, preserving an explicit environment setting. The NVIDIA default
reduces batch-shape numerical variation. This mode's documented hardware
scope is NVIDIA GPUs with compute capability 8.0 or newer; model and attention
backends must also support it. Set `VLLM_BATCH_INVARIANT=0` explicitly before
launch to disable it. Without it, greedy output can vary with batch composition.
Use the same setting for a Target-only comparison. Performance cost has not been
measured, and this setting is not a universal exact-output guarantee.

Sampling supports `temperature`, `top_p`, `top_k` and a Target `seed`.
Draft uses independent random draws. A Target seed does not promise identical
output across different proposal schedules or batch compositions.

The frontend rejects non-default repetition, frequency and presence penalties,
including values inherited from a model's `generation_config.json`. For models
that enable repetition penalties by default, explicitly send
`"repetition_penalty": 1.0` if that is the intended sampling behavior. The current
DT path cannot preserve a non-default penalty; it does not silently discard it.

To run without RDMA, omit `--rdma-host` on both roles. This supports
`temperature: 0` only and advertises `greedy_token_ids`; candidates use HTTP.
A random-sampling request is rejected before admission in this configuration.

## Stop and scope

`POST /drain` closes admission while existing sessions finish. Stop the frontend
streams to cancel active requests. Before graceful process shutdown, wait for
both `active_sessions` and `retained_artifacts` in `/status` to reach zero.
GPU buffers are reused by exact shape and remain registered until shutdown, so
GPU memory can stay at its per-shape concurrency peak after sessions finish.
Unacknowledged publications or uncertain DMA completion can retain registered
memory until process termination; there is no transparent peer-failure recovery.

This implementation handles one Draft and one Target per request, text input,
and linear candidates. Hidden-state methods, trees, cascaded verification,
multimodal input, P/D composition and KV transfer/offload are not implemented.
Neither transfer diagnostics nor successful generation establish a speedup or
complete Kubernetes/autoscaling acceptance.

## Run the two-host diagnostic

Use two Linux hosts with Python 3.10 or later, working RDMA, reachable control and
Mooncake RPC ports, and the engine environment's PyTorch and Mooncake wheels. The transport targets
the APIs in the repository-pinned Mooncake commit
`719735896c86b56fabec6cf3e825fb2ea640597a`. Use the platform-provided wheel matching
your accelerator; installing this package does not replace that wheel or install
vLLM. GPU registration must be supported by the hardware and drivers. Cross-host
A100 validation used `mooncake-transfer-engine-cuda13==0.3.13.post1` with PyTorch
`2.11.0+cu130`. A CUDA 12 Mooncake wheel in a CUDA 13 image is not interchangeable
with the CUDA 13 wheel.

On both hosts, from the repository root:

```bash
python -m pip install --no-deps ./data-plane/dt-plugin
```

Set `PRODUCER_IP` to the producer's routable IP and `CONSUMER_IP` to the consumer's
routable IP. These are local host addresses without ports. On the producer:

```bash
foretoken-dt-transfer serve --host "$PRODUCER_IP" --device cuda:0
```

Once it prints `listening`, run on the consumer:

```bash
foretoken-dt-transfer read --host "$CONSUMER_IP" --peer "$PRODUCER_IP" --device cuda:0
```

Both peers must use diagnostic protocol version 2, whose tensor descriptors carry
dtype and shape as well as byte size.
The producer publishes an int64 tensor; the consumer pulls it into local device
storage, acknowledges the completed read, and checks every element. A successful
consumer prints `verified: true`, the byte count, device, and transfer duration.
Both processes then exit. The diagnostic accepts one consumer, uses TCP port
19090 for descriptors/acknowledgements, and uses Mooncake P2P handshake without a
separate metadata server or Mooncake Store. Tensor data does not use the control
socket.

Use `--nic` to select an RDMA device and `--port` on both commands to change the
control port. `--elements` on the producer changes the payload size. Producer
`--iterations 4` exercises four publications using the same source/destination
registrations, changing contents each time to detect stale data. Output `bytes`
is per transfer; `transfer_seconds` sums the read durations across iterations.
`--device cpu`
is available for host-memory diagnostics; it does not validate GPU direct access.
Control messages assume trusted peers on the deployment network.

## Meaning and limits of a successful run

The transport requests RDMA and has no application-level TCP fallback. Check
Mooncake logs and device/network counters to establish the actual data path;
`requested_transport` alone does not prove GPUDirect RDMA or absence of staging.
The reported duration includes first-connection and polling overhead and is not
a throughput benchmark or DT speedup result.

Diagnostic registrations persist until explicit close. The source cannot be reused before
its read acknowledgement; the destination cannot be reused while a transfer is
pending. Cancellation does not cancel DMA or free memory. Failed or uncertain
transfers retain registrations until their owning process is terminated. A
timeout status continues polling, so a stuck diagnostic requires operator
termination. This is intentional memory ownership, not automatic recovery.

The GPU diagnostic records CUDA events after local tensor work. The transport
waits for those events before publication, overwrite, and unregister, without
requiring unrelated streams to finish. Callers that omit an event retain the
device-wide synchronization path.

[Connector ownership and engine interfaces](docs/connector-contract.md)
