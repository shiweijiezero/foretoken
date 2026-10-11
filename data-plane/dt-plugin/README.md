<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Draft/Target services

English | [简体中文](README_zh.md)

Run independently selected Draft and Target models on separate GPUs or hosts.
The frontend selects workers each round, coordinates proposals and verification, and
streams only Target-confirmed output. Mooncake transfers the Draft's full proposal
distributions between GPU workers; candidate IDs and tensor descriptors use HTTP.
Each engine owns its own scheduler and KV cache.

The commands below start the role services directly for use with the [Rust frontend example](docs/frontend-workflow.md).

## Start two roles

Both hosts need native vLLM `0.31.0`, or the repository's
MetaX source pair (`vllm==0.30.0.dev0`, `vllm-metax==0.29.0.dev0`),
Python 3.10+, compatible PyTorch and Mooncake wheels, and working GPU/RDMA access.
The model-server image includes this package in the engine environment. For a
standalone launch, install it there using the command below; vLLM and the
accelerator-specific Mooncake wheel must already be installed. See the
[integration contract](docs/mrv2-integration.md) for engine compatibility.

For MetaX, use the patched [source runtime](../../deploy/inference-engines/vllm-metax/),
including its FlashAttention sequence-length fix for Model Runner V2. Keep
`VLLM_USE_V2_MODEL_RUNNER=1` explicit: the platform defaults to the older runner.
Use a MACA-compatible Mooncake build for RDMA; the source image sets
`MC_MACA_HOST_TRANSPORT=1`. MetaX greedy output
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
python -m pip install ./data-plane/dt-plugin
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
Use the same setting for a Target-only comparison.

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

## Run a neural Draft pair on NVIDIA

Use the NVIDIA runtime above and a compatible unquantized text Target. For DFlash,
choose a `DFlashDraftModel` checkpoint. Both roles receive the same native
speculative configuration. For example, set `TARGET_MODEL=Qwen/Qwen3-8B` and
`TOKENIZER=Qwen/Qwen3-8B`, then set this on both hosts:

```bash
SPEC_CONFIG='{"method":"dflash","model":"z-lab/Qwen3-8B-DFlash-b16","num_speculative_tokens":15}'
```

On the Draft host:

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role draft \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --speculative-config "$SPEC_CONFIG" \
  --host 0.0.0.0 --rdma-host "$DRAFT_IP"
```

On the Target host:

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role target \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --speculative-config "$SPEC_CONFIG" \
  --host 0.0.0.0 --rdma-host "$TARGET_IP"
```

Select separate GPUs and different HTTP ports when both processes share a host.
The candidate budget comes from `num_speculative_tokens`. Both hosts need access
to the Target and Draft checkpoints. The existing Rust frontend coordinates
Target hidden-state transfer automatically. Both `/status` responses must advertise the same `context_profile`; local checkpoint paths must
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

This implementation handles one Draft and one Target per inference round, text
input, and linear candidates. For explicit Target migration, start both NVIDIA
Targets with RDMA and `--enable-kv-migration`, then use the
[batch example](docs/frontend-workflow.md#move-a-request-between-targets).
Migration currently requires greedy sampling, matching model/tokenizer identities
and compatible full-attention KV layouts. The public scheduler keeps the current
Target; it does not automatically migrate requests. Trees,
cascaded verification, multimodal input, P/D composition and KV offload are not implemented.

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

The diagnostic requests RDMA. Use Mooncake logs and device/network counters to
check GPUDirect RDMA; `requested_transport` alone does not establish the actual
path. Transfer timing includes connection and polling overhead. A stuck transfer
requires terminating both diagnostic processes before reusing their memory.
