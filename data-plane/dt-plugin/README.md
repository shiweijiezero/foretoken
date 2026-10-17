<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Draft/Target services

English | [简体中文](README_zh.md)

Run Draft and Target models on separate GPUs or hosts. Foretoken Router chooses
one compatible Draft and Target per request. The selected Target owns confirmed
prefix and round progression; each engine's native Scheduler owns token budgets,
KV and actual batching. Frontend returns only Target-confirmed output. Multiple
requests advance independently on a fixed N:M worker inventory.

The commands below start the role services directly for use with the [Rust frontend example](docs/frontend-workflow.md).

## Start two roles

Both hosts need native vLLM `0.31.0`, or the repository's
MetaX source pair (`vllm==0.30.0.dev0`, `vllm-metax==0.29.0.dev0`),
Python 3.10+, compatible PyTorch and working GPU access.
The model-server image includes this package in the engine environment. For a
standalone launch, install it there using the command below; vLLM and the
GPU runtime must already be installed. See the
[integration contract](docs/mrv2-integration.md) for engine compatibility.

For MetaX, use the patched [source runtime](../../deploy/inference-engines/vllm-metax/),
including its FlashAttention sequence-length fix for Model Runner V2. Keep
`VLLM_USE_V2_MODEL_RUNNER=1` explicit: the platform defaults to the older runner.
Use a MACA-compatible Mooncake build for RDMA; the source image sets
`MC_MACA_HOST_TRANSPORT=1`. Strict greedy comparisons also need the batch-invariant
configuration described below.

Choose model names or paths for `DRAFT_MODEL` and `TARGET_MODEL`. Both models
must share vocabulary size and token-ID meanings. Set `TOKENIZER` to their shared
tokenizer, normally the Target tokenizer. Default auto works through ZMQ TCP without Mooncake, RDMA devices or a shared
socket directory. The launcher chooses its tensor port automatically.

On each host, install the package from the repository root:

```bash
python -m pip install ./data-plane/dt-plugin
```

On the Draft host:

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role draft \
  --model "$DRAFT_MODEL" --tokenizer "$TOKENIZER" \
  --host 0.0.0.0
```

On the Target host:

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role target \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --host 0.0.0.0
curl http://127.0.0.1:19100/status
```

`/status` should report the expected model and role, `accepting: true`, and
`candidate_format: "token_ids_log_probs"`, `tensor_transports: ["zmq"]` and
`protocol_version: 9` when no RDMA or GPU peer path is available. Both selected roles must advertise the
same format. Start the frontend using their HTTP endpoints; the role services
are internal APIs and do not expose public OpenAI endpoints or authentication.

Each role uses one GPU worker, eager execution and synchronous local
scheduling. Requests waiting for remote proposals do not block other requests.
The role accepts native vLLM model, memory and batching options.
Target `--draft-tokens` selects the default candidates per round (3). Target
`--max-candidate-tokens` sets its startup engine capacity; when omitted it uses
that default. For example, add `--draft-tokens 3 --max-candidate-tokens 8` to the
Target command to reserve capacity for up to eight candidates while requesting
three per round. The default must fit the capacity; the final round is shortened
to the remaining output length.

Ordinary autoregressive Drafts have no configured candidate-count limit: they
execute the count requested by Target, subject to native context and resource
limits. Both options are Target-only. Update frontend and both roles together;
protocol v9 advertises initialized tensor paths for binding-time selection, alongside
separate round policy/capacity.
`--port` changes the HTTP port (default 19100). Select different GPUs and ports
when roles share a host. Their HTTP endpoints must be reachable from the frontend
and the selected Target.

The launcher preserves vLLM's native `VLLM_BATCH_INVARIANT` default (disabled).
For strict greedy token-ID comparisons, explicitly set
`VLLM_BATCH_INVARIANT=1` for both DT roles and the Target-only baseline. This mode
requires compatible model and attention backends. NVIDIA requires compute
capability 8.0 or newer. The pinned MetaX source build includes a smaller native
GEMM tile for its 64 KiB shared-memory limit and persistent kernels for native
PyTorch linear calls, without CUDA-only BLAS controls. BF16 Qwen3-4B verification was checked
with budgets 1 and 3. Without batch invariance, changing batch shape can alter the
logit ordering and greedy output. A token-ID mismatch remains a failed comparison;
close logits alone do not establish correctness. See the
[validation boundary](docs/mrv2-integration.md#greedy-validation).

The token-only path requires `temperature: 0`; random sampling is rejected before
admission. The tensor adapters support `temperature`, `top_p`, `top_k` and
a Target `seed`. Draft uses independent random draws. A Target seed does not
promise identical output across proposal schedules or batch compositions.

The frontend rejects non-default repetition, frequency and presence penalties,
including values inherited from a model's `generation_config.json`. For models
that enable repetition penalties by default, explicitly send
`"repetition_penalty": 1.0` if that is the intended sampling behavior. The current
DT path cannot preserve a non-default penalty; it does not silently discard it.
The DT adapter also rejects `prefixCacheIsolation` configuration and explicit
`cache_salt`; caller-specific prefix-cache namespaces are not supported.

## Automatic tensor transport

The default `--tensor-transport auto` offers ZMQ TCP, allocated Mooncake RDMA,
and MACA GPU IPC when the worker has granted P2P peers. Managed deployments supply the Pod IP and RDMA
allocation; clients do not configure tensor addresses or ports. Standalone roles
use their RDMA address or `--host` address, or vLLM's `VLLM_HOST_IP`/interface selection when
listening on all interfaces. Both peers must be able to reach the advertised
address and dynamically allocated TCP ports.

Router requires a compatible initialized transport. Target prefers GPU IPC for
same-host GPUs with mutual granted peer access, then common RDMA, then ZMQ,
and binds that choice for the request. The
`DT binding` log identifies the request, endpoints and selected path. This is a
capability rule, not a measured performance guarantee. An in-flight transfer never
switches paths or replays on failure; its original owner retains cleanup responsibility.

ZMQ stages probabilities and features through host memory, including D2H and H2D
copies. TCP works between separate pods or hosts without a shared filesystem.
For a deliberately shared same-host socket directory, override Draft with
`--zmq-endpoint ipc:///tmp/foretoken-draft.sock` and Target with
`--zmq-endpoint ipc:///tmp/foretoken-target.sock`. Paths must be unique and visible
at the same absolute location to both workers. IPC is an explicit deployment
choice; auto does not assume that co-located containers share a filesystem.
The ZMQ paths do not provide GPU P2P.

MACA GPU IPC reuses Mooncake's `maca` backend, moving device tensors without the
plugin's host staging. It requires both containers to have device access to the
intended GPU pair; `CUDA_VISIBLE_DEVICES` still selects each role's compute GPU.
GPU permission does not imply ownership: operators must grant only the peers
allocated to that serving group. Foretoken does not expand device access or change
GPU resource assignments. With the current one-GPU-per-Pod isolation, auto keeps
RDMA/ZMQ unless the deployment already grants the required peers. Shared host IPC
namespace alone is insufficient and is not enabled by this integration.

Workers advertise `gpu_ipc: {host_id, device_id, peer_devices}` only with this path.
Routing and live binding require the same host and mutual peer access. Discovery
queries the MACA runtime in a short-lived metadata process without changing the
compute Worker's visibility. Use `--tensor-transport gpu-ipc` to require this path;
missing peer access fails startup. This adapter currently supports MACA only;
NVIDIA CUDA IPC/NVLink is not enabled. Actual link utilization and performance
must be measured; the capability does not claim an NVLink hardware path.

For standalone RDMA, install a compatible Mooncake wheel, grant GPU/RDMA access,
RDMA userspace libraries and sufficient locked memory, then add
`--rdma-host "$DRAFT_IP"` and `--rdma-host "$TARGET_IP"` respectively. Auto offers
both `rdma` and `zmq`; `--rdma-nic` optionally selects an HCA. With a loaded
`nvidia-peermem`, set `WITH_NVIDIA_PEERMEM=1`; otherwise DMA-BUF requires driver
support. In auto mode, a missing Mooncake package or a failed RDMA initialization is logged
and RDMA is omitted from the advertised paths. Forced RDMA fails startup instead. The checks include registration on the actual Worker GPU and run before model
transfers; unrelated errors remain fatal. Before Target prefill, RDMA bindings
share a connection-owned GPU content proof in both directions, reusing it until
the actual Worker transport instance changes or the connection fails; see the [binding proof](docs/connector-contract.md#rdma-binding-proof).
Local initialization alone is not a GPUDirect test.

Use `--tensor-transport rdma` or `--tensor-transport zmq` to force one path for
operations and comparisons. `--tensor-transport none` on both roles preserves the
greedy token-only path; it cannot run neural Drafts or random sampling. Status
advertises `tensor_transports` (empty for token-only); upgrade both roles and
frontend together. RDMA, cross-host TCP and GPU performance require their own
real deployment validation.

## Run a neural Draft pair

Use a supported runtime above and a compatible unquantized text Target. The native
engine must implement the selected Draft method. For DFlash, choose a
`DFlashDraftModel` checkpoint. Both roles identify the same Draft method
and model pair through native speculative configuration. For example, set `TARGET_MODEL=Qwen/Qwen3-8B` and
`TOKENIZER=Qwen/Qwen3-8B`, then set this on both hosts:

```bash
SPEC_CONFIG='{"method":"dflash","model":"z-lab/Qwen3-8B-DFlash-b16","num_speculative_tokens":15}'
```

For a same-host pair using automatic ZMQ TCP, start Draft on GPU 0:

```bash
CUDA_VISIBLE_DEVICES=0 VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role draft \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --speculative-config "$SPEC_CONFIG" \
  --port 19100
```

Start Target on GPU 1:

```bash
CUDA_VISIBLE_DEVICES=1 VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role target \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --speculative-config "$SPEC_CONFIG" \
  --port 19101
```

For cross-host execution add `--host 0.0.0.0` on each role. With RDMA available,
add each host's `--rdma-host` and follow the prerequisites above.
Native capacity comes from `num_speculative_tokens`; Target `--draft-tokens`
selects the round length (default 3). The Draft computes its native proposal block
and publishes the requested prefix with matching probability rows. Both hosts need access
to the Target and Draft checkpoints. Target and Draft coordinate
hidden-state transfer directly. Both `/status` responses must advertise the same `context_profile`; local checkpoint paths must
identify the same pair on both hosts.

The same role commands accept EAGLE/EAGLE3 and single-module MTP profiles. For
EAGLE3 with the same Qwen3-8B Target, use:

```bash
SPEC_CONFIG='{"method":"eagle3","model":"AngelSlim/Qwen3-8B_eagle3","num_speculative_tokens":3}'
```

For Qwen3 DSpark with the same Target, use:

```bash
SPEC_CONFIG='{"method":"dspark","model":"deepseek-ai/dspark_qwen3_8b_block7","num_speculative_tokens":7}'
```

DSpark uses native Markov sampling and transfers its conditional proposal
probabilities in Target vocabulary order. Use a `Qwen3DSparkModel` checkpoint
and fixed-length verification (`enable_adaptive_verification=false`, the native
default). Confidence-based adaptive verification is not enabled for remote drafts.

For MiMo MTP, set both `TARGET_MODEL` and `TOKENIZER` to `XiaomiMiMo/MiMo-7B-Base`,
add `--trust-remote-code` to both role commands for its configuration class, and use:

```bash
SPEC_CONFIG='{"method":"mtp","num_speculative_tokens":3}'
```

Neural drafting supports greedy and random sampling and chunked prefill. Target
prefix caching is disabled; EAGLE/MTP also disable Draft prefix caching. Each
round transfers the complete computed feature prefix, increasing the cost of long
contexts. Supported model variants and restrictions are listed in the
[integration contract](docs/mrv2-integration.md#neural-draft-context).

## Stop and scope

`POST /drain` closes admission while existing sessions finish. Stop the frontend
streams to cancel active requests. Before graceful process shutdown, wait for
both `active_sessions` and `retained_artifacts` in `/status` to reach zero.
Registered buffers remain allocated for reuse until shutdown. A failed peer or
uncertain DMA completion may require process termination; see
[memory and cancellation ownership](docs/connector-contract.md#memory-cancellation-and-drain).

Each Target pool declares compatible Draft pools. Router selects the binding once
through the configured routing pipeline. This delivery uses one Draft per request,
one linear candidate chain and a configurable Target default candidate count. Multi-Draft
collaboration, trees, cascaded verification, multimodal input, P/D composition,
KV offload and automatic migration are not implemented.
The internal [checkpoint/resume protocol](docs/role-protocol.md#target-kv-checkpoint-and-resume)
is available for manual migration diagnostics with NVIDIA RDMA
(`--tensor-transport rdma --enable-kv-migration`), greedy sampling,
matching model/tokenizer identities and compatible full-attention KV layouts.

## Run the two-host diagnostic

Run this diagnostic in the same engine and RDMA environment as the roles.

Set `PRODUCER_IP` to the producer's routable IP and `CONSUMER_IP` to the consumer's
routable IP. These are local host addresses without ports. On the producer:

```bash
foretoken-dt-transfer serve --host "$PRODUCER_IP" --device cuda:0
```

Once it prints `listening`, run on the consumer:

```bash
foretoken-dt-transfer read --host "$CONSUMER_IP" --peer "$PRODUCER_IP" --device cuda:0
```

The consumer prints `verified: true`, byte count, device and transfer duration,
then both processes exit. Control traffic uses TCP port 19090; allow this port and
Mooncake's dynamically allocated handshake ports between the hosts.

Use `--nic` to select an RDMA device and `--port` on both commands to change the
control port. `--elements` on the producer changes the payload size. Producer
`--iterations 4` exercises four publications using the same source/destination
registrations, changing contents each time to detect stale data. Output `bytes`
is per transfer; `transfer_seconds` sums the read durations across iterations.
`--device cpu`
is available for host-memory diagnostics; it does not validate GPU direct access.
Control messages assume trusted peers on the deployment network.

The diagnostic requests RDMA by default. Add `--tensor-transport gpu-ipc` on both
commands to check granted same-host MACA peers, or `--tensor-transport zmq` to
measure ZMQ TCP with the same tensor sizes and reuse. Use Mooncake logs and device/network counters to
check GPUDirect RDMA; `requested_transport` alone does not establish the actual
path. Transfer timing includes connection and polling overhead. A stuck transfer
requires terminating both diagnostic processes before reusing their memory.
