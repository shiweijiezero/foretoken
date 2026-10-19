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

Values are YAML booleans, numbers, strings, lists or objects. Omit an option or set it to `null` to use its default. Supported values depend on the backend image, model and hardware.

## Model environment variables

Use `spec.env` to set environment variables for one model's runtime processes:

```yaml
spec:
  env:
    OMP_NUM_THREADS: 8
    VLLM_LOGGING_LEVEL: DEBUG
    TOKENIZERS_PARALLELISM: false
```

Values accept strings, numbers and booleans, converted to strings for the process. Use quotes to preserve text such as `"001"`. `$(NAME)` references another variable regardless of declaration order; references must not form a cycle.

Each Pool inherits its model's `spec.env`. Set `modelPools[].env` to replace the complete mapping for one Pool, or `env: {}` to clear its inherited custom variables. Omission or `null` inherits the model's settings.

For credentials, use a Secret reference rather than a literal value:

```yaml
spec:
  env:
    AWS_ACCESS_KEY_ID:
      valueFrom:
        secretKeyRef:
          name: model-storage
          key: access-key-id
```

`valueFrom` supports Kubernetes Secret, ConfigMap, Pod-field and container-resource references. In this example, create Secret `model-storage` with key `access-key-id` in the model's namespace. Literal values are saved with deployment and experiment configuration.

These settings are local to the model, including all members of a multi-node replica. Build and separate model-download settings use their existing configuration. Foretoken-managed startup, topology and cache variables remain platform-controlled.

Redeploy the configuration directory with `foretoken deploy <directory>` to replace instances whose environment changed. Updating Secret or ConfigMap contents alone takes effect when a new container starts; to apply it immediately, change the reference and redeploy.

## Select serving nodes

Use a Kubernetes label to place a service or Pool on a machine group. Assign the same key/value to every node in the group:

```bash
kubectl label node <node-a> workload-group=group-a --overwrite
kubectl label node <node-b> workload-group=group-a --overwrite
kubectl get nodes -L workload-group
```

Use that same key/value in the model YAML:

```yaml
spec:
  nodeSelector:
    workload-group: group-a
```

`ModelService.spec.nodeSelector` selects the default Pool. `modelPools[].nodeSelector` selects a different group for one Pool. The cluster scheduler places serving and runtime-preparation Pods on nodes carrying the selected label.

## Instance concurrency

To bound accepted, unfinished work at each text model-server instance, including its engine queue, add this setting to the model YAML:

```yaml
spec:
  instanceAdmission:
    maxConcurrentRequests: 32
```

Redeploy the model's configuration directory with `foretoken deploy`. Limits update online and accepted work continues. A full instance stops accepting new work until occupancy falls below the limit. Omit `instanceAdmission` for unlimited acceptance.

## Prefix cache isolation

Prefix caching reuses computation for repeated prompt prefixes. To separate this cache by authenticated caller, add the following to the ModelService YAML:

```yaml
spec:
  prefixCacheIsolation: true
```

The setting applies to all Pools and frontends serving this model, across Chat Completions, text completions, Responses and Messages. It supports vLLM text and image generation.

| Value | Cache sharing |
| --- | --- |
| `true` | Isolate prefixes by authenticated caller, ignoring the request body's `cache_salt`. |
| `false` | Share unsalted prefixes across callers, ignoring the request body's `cache_salt`. |
| Omitted or `null` | Use the request body's `cache_salt`; omission, `null` or an empty string uses unsalted prefixes. |

Isolation requires an authentication gateway. Changing isolation leaves prefix caching and admission settings unchanged.

Apply the model and frontend configuration directory with `foretoken deploy <directory>`. Changing this setting replaces model instances and switches to a separate cache domain, including for persistent shared caches.

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
| `enforce-eager` | Disable graph capture when `true`; defaults to `true` for Encoder Pools |
| `enable-prefix-caching` | Enable KV prefix caching with `true` or disable it with `false`; Encoder Pools use `false` |
| `speculative-config` | Native speculative decoding configuration |
| `tensor-parallel-size` | Tensor parallelism (TP) |
| `pipeline-parallel-size` | Pipeline parallelism (PP) |
| `data-parallel-size` | Data parallelism (DP) within one model replica |
| `prefill-context-parallel-size` | Prefill context parallelism (PCP) |
| `decode-context-parallel-size` | Decode context parallelism (DCP), reusing existing ranks |

`nodes` selects how many Kubernetes nodes each model replica uses; `resources.requests.gpu.count` is the GPU count per member Pod. Their product must equal TP × PP × DP × PCP for vLLM. DCP does not add GPUs. Expert parallelism uses native `enable-expert-parallel`, `all2all-backend` and `enable-eplb` options.

Model replicas can span nodes. Foretoken places one member on each node and manages startup, readiness and restart as a complete group. `foretoken install` prepares the LeaderWorkerSet controller and RDMA allocation; communication libraries select from allocated devices. A persistent cache used across nodes must be accessible from every member.

P/D and E/P/D Pools can select their own parallelism settings within the model and engine's supported combinations. PCP and DCP support also depends on the attention backend. The [EPD runtime](../../examples/feature/encoder-prefill-decode/README.md) includes CP-aware Mooncake transfer: Prefill and Decode must use matching PCP/DCP cache layouts, and their TP sizes must divide one another.

`modelPools[].engineArgs`, when supplied, replaces the service-level native options for that Pool. Service replica counts remain separate from engine data parallelism.

Expert parallelism (EP) keeps each DP rank's KV cache separate. Single-node EP requests RDMA devices only when its active communication backend requires them.

## Prefill/decode separation and KV reuse

Prefill processes the input; Decode generates the answer. With NIXL, a later request can also reuse the KV cache retained by a previous Decode instance. Requests may reach different frontends and Prefill instances; reuse depends on matching input tokens and cache isolation, not a session identifier.

For a service with `prefill` and `decode` entries in `modelPools`, select their shared transport in the model YAML:

```yaml
spec:
  kvTransfer:
    connector: nixl
    protocol: tcp
```

The defaults are `connector: mooncake` and `protocol: tcp`. Choose `protocol: rdma` to use RDMA devices allocated by the platform. Apply changes with `foretoken deploy <directory>`; this replaces the model's P/D instances while other services keep their own transport choices.

The engine environment must include NIXL and UCX built for the selected accelerator. Decode-to-Prefill reuse requires a Foretoken-supported vLLM release of 0.31 or later and native device-buffer transfer support. Foretoken enables it automatically for full-attention text models without LoRA; other supported configurations retain ordinary P/D behavior. NIXL currently requires one node per model replica, matching P/D TP sizes and KV layouts, and PP/PCP/DCP of 1. Encoder separation and Mooncake Store use Mooncake.

The [NIXL example](../../examples/feature/nixl-prefix-reuse) serves `Qwen/Qwen2.5-0.5B-Instruct`, requesting one GPU per stage and two frontend replicas. It selects TCP in the model configuration and reuses the Quick Start's storage. On a prepared Foretoken cluster, run from the repository root:

```bash
foretoken deploy examples/feature/nixl-prefix-reuse --timeout 20m
export FRONTEND_URL="$(foretoken endpoint examples/feature/nixl-prefix-reuse)"

python3 - <<'PY'
import json
import os
from urllib.request import Request, urlopen

messages = []
for question in (
    "Explain prompt caching in detail, with examples and its effect on latency.",
    "How does this help when a conversation continues on another server?",
):
    messages.append({"role": "user", "content": question})
    request = Request(
        os.environ["FRONTEND_URL"] + "/v1/chat/completions",
        data=json.dumps({
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "messages": messages,
            "temperature": 0,
            "max_tokens": 256,
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=300) as response:
        answer = json.load(response)["choices"][0]["message"]["content"]
    print(answer, "\n")
    messages.append({"role": "assistant", "content": answer})
PY
```

For subsequent turns, send the complete conversation history through the same API. Chat templates that remove earlier reasoning change the token prefix and require recomputation. A matching retained prefix extends Prefill's local cache when the engine finds the transfer worthwhile; otherwise Prefill computes normally. Load-based routing also uses this reuse path. [Routing weights](../../data-plane/frontend/src/router/README.md) control target preference, not whether reuse is available.

NIXL selects network devices automatically. If explicit selection is needed, set `spec.env.UCX_NET_DEVICES` to UCX identifiers such as `mlx5_0:1,mlx5_1:1`.

Remove the example when finished:

```bash
foretoken delete examples/feature/nixl-prefix-reuse
```

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

Use `spec.model`, `spec.tokenizer`, `nodes`, `kvCache`, `kvTransfer` and `ecProfile` for model selection, placement, cache storage and transfer. Foretoken supplies the corresponding startup settings. Profiling owns its native configuration when a runtime cache is enabled; mcTracer also selects its worker class. Multi-node replicas use the `mp` executor.

Other native options, including `ec-manager-config` and custom workers outside mcTracer, are passed to vLLM. See the [vLLM argument reference](https://docs.vllm.ai/en/latest/configuration/engine_args/).
