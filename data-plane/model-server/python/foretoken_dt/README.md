<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Draft–Target disaggregation

English | [简体中文](README_zh.md)

Run vLLM's Target model and EAGLE drafter on separate GPUs. Target serves the
OpenAI-compatible API and retains native scheduling, verification and sampling;
Draft generates candidates with its own KV cache. This experimental adapter is
enabled explicitly through `--worker-cls` and `--additional-config`.

## Start a local pair

Use Linux with two CUDA GPUs and vLLM built from commit
`1be36283678a9a94fc8fdaad6c95c2896d6b4015`, the source revision in
[`data-plane/Cargo.toml`](../../../Cargo.toml). Use the
[vLLM GPU installation guide](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/#build-wheel-from-source)
with that revision when preparing the environment. Both processes need access to
the Target configuration and Draft checkpoint. The adapter uses vLLM's existing
PyTorch, NumPy, PyZMQ and NCCL dependencies.

The following standalone setup has been validated on a same-host A100 pair with
`Qwen/Qwen3-8B` and `Tengyunw/qwen3_8b_eagle3`, proposing three tokens per round.
The adapter is not integrated into Foretoken's Kubernetes deployment. The default
model-server image uses a different vLLM revision; use the pinned environment
above for these commands.

From the repository root, start Draft in one terminal:

```sh
CUDA_VISIBLE_DEVICES=1 \
PYTHONPATH="$PWD/data-plane/model-server/python${PYTHONPATH:+:$PYTHONPATH}" \
python -m foretoken_dt --listen tcp://127.0.0.1:5557
```

Draft waits for Target to send its configuration and shared vocabulary weights.
In a second terminal, also at the repository root, start Target:

```sh
CUDA_VISIBLE_DEVICES=0 VLLM_USE_V2_MODEL_RUNNER=1 \
PYTHONPATH="$PWD/data-plane/model-server/python${PYTHONPATH:+:$PYTHONPATH}" \
vllm serve Qwen/Qwen3-8B \
  --worker-cls foretoken_dt.target.TargetWorker \
  --speculative-config '{"model":"Tengyunw/qwen3_8b_eagle3","method":"eagle3","num_speculative_tokens":3}' \
  --additional-config '{"foretoken_dt":{"endpoint":"tcp://127.0.0.1:5557","early_feature_send":true}}' \
  --dtype bfloat16 --no-async-scheduling --no-enable-prefix-caching \
  --max-model-len 1024 --max-num-seqs 16 --max-num-batched-tokens 128
```

Wait for `Draft model and KV cache ready` and Target's API startup, then send a
request from another terminal:

```sh
curl --fail-with-body --no-buffer http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"Explain speculative decoding briefly."}],"temperature":0,"max_tokens":128,"stream":true}'
```

Target streams the generated output. Stop both processes with Ctrl-C. Restart
both for a new session or after a failed proposal.

## Configuration and limits

One Draft process serves one Target process, with one GPU per process. Supported
models use EAGLE or EAGLE3, unquantized text weights, fixed speculation length and
greedy Draft proposals. Target can use greedy or random sampling and structured
output. Chunked prefill and multiple requests per batch are supported.

Scheduling must be synchronous and prefix caching must be disabled. KV cache
dtype must be `auto` on both workers, and both use the same maximum context
length. Tensor, pipeline, data and context parallelism, multimodal input, LoRA,
KV transfer, parallel drafting, dynamic speculation length, online weight updates
and sleep mode are unsupported.

Set adapter options inside the `foretoken_dt` object in `--additional-config`:

| Option | Default | Purpose |
| --- | --- | --- |
| `endpoint` | Required | Draft's ZeroMQ control endpoint, matching `--listen`. |
| `transport` | `"nccl"` | CUDA tensor transfer through NCCL; `"tcp"` stages tensors through CPU memory. |
| `early_feature_send` | `false` | With EAGLE3 and NCCL, overlap eligible Draft input preparation and feature transfer with Target computation. The example enables this option. |
| `timeout_seconds` | `300` | Deadline for each socket wait or CUDA transfer completion. Increase it for slow initial model loading. |

The TCP transport and original EAGLE method require `early_feature_send` to be
`false`. NCCL chooses the physical link from the available topology and runtime
configuration. Cross-host RDMA has not been validated for this adapter. The Draft
endpoint has no authentication; keep it on a trusted private network.

Draft derives its KV allocation from available GPU memory after profiling
activation usage. To select an explicit pool size, add
`--kv-cache-memory-bytes BYTES` to the Draft command. CUDA graph capture also needs
memory; reduce the KV pool or capture sizes if capture exhausts GPU memory.

A detected timeout or failed proposal ends the session without retrying. The
transfer deadline cannot interrupt a blocking native NCCL initialization or
submission call; stop both processes if one remains stuck there.

## Compare with colocated execution

Stop the pair, then rerun Target without `--worker-cls` and `--additional-config`
to use native colocated EAGLE. Keep the model pair, dtype, speculation length,
batch limits and workload unchanged. Compare complete generated outputs as well
as latency and throughput using the [benchmark workflow](../../../../benchmarks/README.md).
Keep profiling runs separate from performance measurements.
