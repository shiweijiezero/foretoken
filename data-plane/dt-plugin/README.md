<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken DT roles and transport

English | [简体中文](README_zh.md)

This package runs independent greedy Draft and Target roles and provides a
Mooncake tensor diagnostic. Target requires the separate vLLM
`feat/external-speculation` extension described in the
[integration contract](docs/mrv2-integration.md); the repository-pinned engine
cannot run this role. Installation does not patch vLLM or register a `vllm serve`
mode. The [Rust frontend workflow](docs/frontend-workflow.md) has a runnable
token-input example and public API dispatch. The experimental
[Kubernetes example](../../examples/draft-target/README.md) uses controller-owned
role deployment, discovery and bounded drain. It requires the independent engine
extension in the platform runtime image.

## Start a role

Use Python 3.10+ with the appropriate vLLM engine, PyTorch, FastAPI, Uvicorn,
AnyIO and Pydantic 2 installed. In that environment, from the repository root:

```bash
python -m pip install --no-deps ./data-plane/dt-plugin
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role draft --model "$DRAFT_MODEL"
```

On the Target host, with the independent engine extension installed:

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role target --model "$TARGET_MODEL"
curl http://127.0.0.1:19100/status
```

Choose compatible text models with identical token-ID meanings. The initial path
supports greedy linear candidates and a single Target worker; Target uses eager
execution and disables local asynchronous scheduling. Waiting for candidates
holds only the affected request. Standard vLLM model, memory and batching flags
are accepted; `--draft-token-budget` controls the candidate limit (default 3).
Use `--host` and `--port` for trusted-network access; the default binds localhost.
There is no authentication or public OpenAI endpoint on this internal service.

The frontend must drive the [role protocol](docs/role-protocol.md). Draft uses a
normal vLLM request per round and reuses prefix caching; it does not retain a
manually managed live Draft KV cursor. Closing the owning stream aborts the
session. `POST /drain` rejects new sessions while allowing existing rounds;
close existing streams before stopping the server.

Small greedy token candidates use HTTP control messages. This execution path
neither transfers KV nor uses Mooncake for candidate tokens. The tensor transport
below is independently usable and validated; stochastic proposals, tensor-bearing
methods, multimodal input, PD composition and automatic scaling remain outside
this initial role implementation. A successful two-role run is not evidence of a
speedup.

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

On systems using the loaded `nvidia-peermem` kernel module, set
`WITH_NVIDIA_PEERMEM=1` in both process environments. Otherwise Mooncake's default
DMA-BUF path requires corresponding driver support. Containers also need GPU and
RDMA device access, the RDMA userspace libraries, and sufficient locked memory.

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

Registrations persist until explicit release. The source cannot be reused before
its read acknowledgement; the destination cannot be reused while a transfer is
pending. Cancellation does not cancel DMA or free memory. Failed or uncertain
transfers retain registrations until their owning process is terminated. A
timeout status continues polling, so a stuck diagnostic requires operator
termination. This is intentional memory ownership, not automatic recovery.

The GPU diagnostic records CUDA events after local tensor work. The transport
waits for those events before publication, overwrite, and unregister, without
requiring unrelated streams to finish. Callers that omit an event retain the
device-wide synchronization path.
Successful tensor diagnostics do not validate speculative decoding or its performance.

[Connector contract and remaining engine interfaces](docs/connector-contract.md)
