<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Inference parameters

English | [简体中文](inference-parameters_zh.md)

Configure the selected engine through `ModelService.spec.engineArgs`, using native option names without `--`:

```yaml
spec:
  model: Qwen/Qwen2.5-0.5B-Instruct-AWQ
  backend: vllm
  engineArgs:
    quantization: awq
    dtype: half
    max-model-len: 8192
    tensor-parallel-size: 1
    gpu-memory-utilization: 0.85
```

Values are YAML booleans, numbers, strings, lists or objects. Omitted options retain engine defaults; `null` omits a native option. Supported values depend on the backend image, model and hardware.

## Common vLLM options

| Engine option | Purpose |
| --- | --- |
| `max-model-len` | Maximum combined input and output token count |
| `dtype` | Model compute precision |
| `quantization` | Weight quantization method |
| `kv-cache-dtype` | KV cache precision, separate from weight quantization |
| `gpu-memory-utilization` | Fraction of device memory per engine instance |
| `max-num-seqs` | Maximum sequences scheduled per iteration |
| `max-num-batched-tokens` | Maximum tokens scheduled per iteration |
| `enforce-eager` | Disable graph capture when `true` |
| `speculative-config` | Native speculative decoding configuration |
| `tensor-parallel-size` | Tensor parallelism (TP) |
| `pipeline-parallel-size` | Pipeline parallelism (PP) |
| `data-parallel-size` | Data parallelism (DP) within one model replica |
| `prefill-context-parallel-size` | Prefill context parallelism (PCP) |
| `decode-context-parallel-size` | Decode context parallelism (DCP), reusing existing ranks |

`nodes` selects how many Kubernetes nodes each model replica uses; `resources.requests.gpu.count` is the GPU count per member Pod. Their product must equal TP × PP × DP × PCP for vLLM. DCP does not add GPUs. Expert parallelism uses native `enable-expert-parallel`, `all2all-backend` and `enable-eplb` options.

Aggregated replicas can span nodes. Foretoken places one member on each node and manages startup, readiness and restart as a complete group. `foretoken install` prepares the LeaderWorkerSet controller and RDMA allocation; communication libraries select from allocated devices. A persistent cache used across nodes must be accessible from every member. Multi-node execution currently requires PCP=1; split serving remains single-node and single-rank.

`modelPools[].engineArgs`, when supplied, replaces the service-level native options for that Pool. Service replica counts remain separate from engine data parallelism.

With EP enabled, attention can use TP × DP while routed experts span the corresponding EP group. Sharing experts does not share attention KV caches: routing selects a model group and a DP rank, using rank-local prefix-cache observations. Missing or interrupted KV event streams are treated as unknown locality, not cache hits; other ranks retain their observations. Load scoring uses each DP rank's running requests, waiting requests and KV utilization from the same telemetry snapshot; group totals remain available for autoscaling. With Mooncake Store enabled, the selected rank's native connector checks shared prefixes across all required cache shards.

## Speculative decoding

Keep the complete native dictionary together:

```yaml
spec:
  model: Qwen/Qwen3-0.6B
  backend: vllm
  engineArgs:
    speculative-config:
      method: ngram
      num_speculative_tokens: 2
      prompt_lookup_max: 4
```

Methods and child fields follow vLLM. For methods using draft weights, `model` accepts a Hub ID or a container-visible absolute directory. vLLM downloads, loads and caches the draft; `spec.source: modelscope` applies to both target and draft Hub IDs.

## Platform-managed options

Foretoken manages model identity, startup endpoints, transfer connectors and profiling. Other native options are interpreted by the selected engine. See the [vLLM argument reference](https://docs.vllm.ai/en/latest/configuration/engine_args/); vLLM is the currently implemented backend.
