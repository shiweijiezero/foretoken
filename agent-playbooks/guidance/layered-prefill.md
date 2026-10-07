# Layer-Group Prefill for MoE

English | [简体中文](layered-prefill_zh.md)

In colocated MoE prefill/decode serving, advancing prefill through contiguous layer groups can reduce repeated expert-weight reads caused by small token chunks while preserving decode opportunities for existing requests.

## Idea

### Why split prefill

Prefill processes a request's input sequence and builds each layer's KV cache; decode then generates tokens one at a time. When both share the same GPUs, a newly arriving long prompt can occupy the devices long enough to delay ongoing decode requests.

Chunked prefill splits the input along the token dimension. Each iteration processes a small input chunk while advancing ongoing decode requests. This shortens individual iterations, but every input chunk still traverses all model layers.

For MoE, this can add expert-weight reads. Each token selects only a few experts, yet the tokens in even a small chunk may collectively select most experts. Consecutive chunks therefore read many of the same weights while assigning few tokens to each expert. These are primarily GPU reads from device memory; the weights can remain GPU-resident throughout.

### Split along layers instead

Layered prefill retains a larger input batch and divides the model into contiguous layer groups. A new request traverses one group per iteration and resumes from that group's output in the next iteration. Existing decode requests still traverse all layers each iteration to produce their next token.

Consider a four-layer model and an eight-token input. Token chunking uses four tokens per chunk; layer grouping uses two layers per group. The table shows only the new request's prefill work:

| Iteration | Token chunks | Layer groups |
| --- | --- | --- |
| 1 | Tokens 1–4 traverse layers 1–4 | Tokens 1–8 traverse layers 1–2 |
| 2 | Tokens 5–8 traverse layers 1–4 | Tokens 1–8 resume from saved state through layers 3–4 |

With layer grouping, layers 1–2 process both prefill and decode in the first iteration, while layers 3–4 process decode only. The roles reverse in the second iteration. The new request produces its first output token after completing all groups. Both schedules execute every input token through every layer; they distribute that work differently across iterations.

### Why MoE can benefit

Consider one MoE layer in the table. Token chunking processes the input in two visits to that layer, potentially reading the same expert weights in both visits. Layer grouping processes all eight input tokens in one visit, allowing each weight read to serve more tokens.

The benefit depends on expert overlap between chunks and how efficiently each expert calculation reuses its weights. The reduction is in repeated memory traffic caused by small chunks, not in the model computation required for each token. Ongoing decode requests still read the weights of their selected experts.

Very long inputs can combine both methods: divide the input into larger token chunks, then advance each chunk through layer groups. This controls intermediate-state memory while retaining larger per-expert batches.

## Applicable scenarios

- Colocated prefill and decode, with arriving long prompts and latency constraints on ongoing decode requests.
- Small token chunks activate many experts, making expert-weight reads expensive; smaller decode batches make this worth investigating.

Pausing and resuming by layer is not tied to one GPU, but the reported gains depend on MoE access patterns. The authors observed regressions for dense Qwen3-8B. Relaxing inter-token latency constraints and allowing larger token chunks also narrows the advantage. A lone request in pure decode, with no arriving prefill, has no extra prefill weight loads for this mechanism to eliminate.

## Implementation pointers

The [public implementation](https://github.com/scale-snu/layered-prefill/tree/053f80e5201a7c0ab56e468e3d578907a2ca9cc3) uses Nano-vLLM and provides scheduling and benchmark commands.

vLLM expresses scheduled work as per-request token counts in [scheduler output](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/vllm/v1/core/sched/output.py). Its [Qwen3 MoE forward](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/vllm/model_executor/models/qwen3_moe.py) traverses the layers assigned to the current pipeline stage. Layer-group scheduling needs to pass per-request layer progress through these interfaces and retain the state needed to resume across iterations.

A port would need the following state and execution changes:

1. Track token progress and layer-group progress separately. Completing a group does not mean that the input has traversed the entire model.
2. Allow the model execution entry point to run a selected layer range. Retain the group output hidden states, required residuals, positions, and KV mappings for the next group. Completed layers keep their KV caches; later layers still await prefill.
3. Include this cross-iteration state in preemption, cancellation, batch regrouping, and CUDA Graph management. Larger groups add more prefill work to an iteration and can delay decode; smaller groups add iterations and state-management overhead. Measure first-token latency, inter-token latency, and intermediate-state memory together.

Compare token-chunk and layer-group scheduling on identical request traces. Measure TTFT, tail inter-token latency, peak memory, and expert-weight reads, holding model and sampling settings constant and checking output agreement. Measure end-to-end gains under mixed serving load.

## References

- [From Tokens to Layers: Redefining Stall-Free Scheduling for MoE Serving with Layered Prefill, v2](https://arxiv.org/html/2510.08055v2). Main experiments use two H100 80GB GPUs, NVLink, and TP2, with Qwen3-30B-A3B and GPT-OSS-20B on ShareGPT/arXiv serving workloads. Counting bytes loaded for expert weights, the authors report reductions of 12% and 39% for the two workload families.
- [Layered Prefill implementation and reproduction instructions](https://github.com/scale-snu/layered-prefill/tree/053f80e5201a7c0ab56e468e3d578907a2ca9cc3), including dependencies, a FlashAttention patch, serving, and measurement commands.
