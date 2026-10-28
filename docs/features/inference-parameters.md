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

## Schedule multi-node replicas

A platform administrator can select [Volcano](https://github.com/kubernetes-sigs/lws/tree/main/docs/examples/sample/gang-scheduling) for complete-group placement, [Kueue](https://kueue.sigs.k8s.io/docs/tasks/run/leaderworkerset/) for quota admission, or both. Foretoken continues to own model capacity, readiness and request draining. Single-node replicas and CPU-only model preparation keep their ordinary scheduling path.

The native controllers and queues must already exist. For the combined example, prepare Volcano Queue `inference`, enable Kueue's LeaderWorkerSet integration and create LocalQueue `inference` in each service namespace. Save this platform selection in `deploy/platform-values.yaml`:

```yaml
workload:
  leaderWorkerScheduling:
    volcanoQueueName: inference
    kueueLocalQueueName: inference
```

Omit either field to use only the other integration. Run `foretoken install --values deploy/platform-values.yaml`, retaining the original source or registry options, then redeploy the instances that should adopt the queues. Foretoken enables Volcano support in its managed LeaderWorkerSet controller; an independently installed controller remains under its administrator's control.

Platform updates do not change existing or stopped instances. Their selected queues remain fixed until the next explicit `foretoken deploy`. Use `leaderWorkerScheduling: null` to clear the selection for new deployments.

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

Prefill processes the input; Decode generates the answer. Mooncake and NIXL can also reuse a previous Decode instance's retained KV cache for a later Prefill. Requests may reach different frontends and Prefill instances; reuse depends on matching input tokens and cache isolation, not a session identifier.

For a service with `prefill` and `decode` entries in `modelPools`, Mooncake is the default connector. Foretoken uses RDMA when both stages can use a compatible device allocation and network; otherwise it selects TCP. Network preparation is part of [platform installation](../deployment/kubernetes.md#rdma-networking), so model configuration needs no NIC or port settings.

The native transport uses GPUDirect RDMA—direct NIC access to GPU memory—when supported by the cluster's GPU and NIC drivers, without a model-level switch.

To use NIXL, select only the connector:

```yaml
spec:
  kvTransfer:
    connector: nixl
```

Set `kvTransfer.protocol` to `tcp` or `rdma` only when overriding automatic selection. Apply changes with `foretoken deploy <directory>`; this replaces the model's P/D instances while other services keep their own transport choices.

Reuse is enabled automatically for full-attention text models without LoRA in the Foretoken vLLM 0.31–0.32 and supplied MetaX environments. These environments include the required transport dependencies.

Reuse requires one node per model replica, compatible P/D KV layouts, and PP/PCP/DCP of 1. NIXL also requires matching P/D TP sizes. Other supported configurations retain ordinary P/D behavior. Encoder separation and Mooncake Store use Mooncake.

The NIXL example declares `Qwen/Qwen2.5-0.5B-Instruct` and its P/D Pools directly in [`model.yaml`](../../examples/feature/nixl-prefix-reuse/model.yaml), leaving network transport selection automatic. It requests one GPU per stage and two frontend replicas, using the [platform's default storage](model-storage.md). On a prepared Foretoken cluster, run from the repository root:

```bash
foretoken deploy examples/feature/nixl-prefix-reuse --timeout 20m
FRONTEND_URL="$(foretoken endpoint qwen-nixl)"
FRONTEND_HOST="$(foretoken endpoint qwen-nixl --host)"

curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FRONTEND_HOST" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [{"role": "user", "content": "Explain how prompt caching reduces latency."}],
    "temperature": 0,
    "max_tokens": 256
  }'
```

For subsequent turns, send the complete conversation history through the same API. Chat templates that remove earlier reasoning change the token prefix and require recomputation. A matching retained prefix extends Prefill's local cache when the engine finds the transfer worthwhile; otherwise Prefill computes normally. Load-based routing also uses this reuse path. [Routing weights](../../data-plane/frontend/src/router/README.md) control target preference, not whether reuse is available.

Use [`foretoken perf`](../../benchmarks/docs/perf/README.md) to measure performance, including [multi-turn conversation workloads](../../benchmarks/docs/perf/conversations.md).

Remove the example when finished:

```bash
foretoken delete qwen-nixl
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
