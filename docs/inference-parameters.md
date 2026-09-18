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
| `tensor-parallel-size` | Tensor parallelism (TP) |
| `pipeline-parallel-size` | Pipeline parallelism (PP) |
| `data-parallel-size` | Data parallelism (DP) within one model replica |
| `prefill-context-parallel-size` | Prefill context parallelism (PCP) |
| `decode-context-parallel-size` | Decode context parallelism (DCP), reusing existing ranks |

Request GPUs through `resources.requests.gpu.count` to match the engine worker count: TP × PP × DP × PCP for vLLM. DCP does not add GPUs. Replicas currently run on one node; split serving requires single-rank execution. Expert parallelism uses native `enable-expert-parallel`, `all2all-backend` and `enable-eplb` options.

`modelPools[].engineArgs`, when supplied, replaces the service-level native options for that Pool. Service replica counts remain separate from engine data parallelism.

## Speculative decoding

Keep the complete native dictionary together:

```yaml
spec:
  model: Qwen/Qwen3-0.6B
  backend: vllm
  speculativeDecoding:
    method: ngram
    num_speculative_tokens: 2
    prompt_lookup_max: 4
```

Methods and child fields follow vLLM. For methods using draft weights, `model` accepts a Hub ID or a container-visible absolute directory. vLLM downloads, loads and caches the draft; `spec.source: modelscope` applies to both target and draft Hub IDs.

## Explicit service fields

The convenience fields `maxModelLen`, `dtype`, `quantization`, `kvCacheDType`, `gpuMemoryUtilization`, `maxNumSeqs`, `maxNumBatchedTokens` and `enforceEager` also work directly under `spec`. Explicit values, including `false`, override their native equivalents. `speculativeDecoding` replaces the whole `engineArgs.speculative-config` dictionary rather than merging child fields.

Foretoken manages model identity, startup endpoints, transfer connectors and profiling. Other native options are interpreted by the selected engine. See the [vLLM argument reference](https://docs.vllm.ai/en/latest/configuration/engine_args/); vLLM is the currently implemented backend.
