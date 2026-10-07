# Layer-Group Prefill for MoE

English | [简体中文](layered-prefill_zh.md)

In colocated MoE prefill/decode serving, advancing prefill through contiguous layer groups can reduce repeated expert-weight reads caused by small token chunks while preserving decode opportunities for existing requests.

## Idea

Conventional chunked prefill splits a prompt along the token dimension. Each iteration processes part of the prompt through every model layer. In fine-grained MoE models, even a small chunk may activate most experts while assigning few tokens to each. Subsequent chunks must read those expert weights again at the same layer.

Layered prefill changes the scheduling dimension: a complete prompt advances through one contiguous group of layers, retains intermediate state, and resumes at the next group in a later iteration. Existing decode requests still traverse all layers each iteration.

```text
token chunks: chunk 1 → all layers; chunk 2 → all layers
layer groups: full prompt → group 1; full prompt → group 2
```

Processing more prompt tokens together at an MoE layer increases reuse of each expert-weight load. Very long prompts can still use larger token chunks, with each chunk advancing through layer groups.

## Applicable scenarios

- Colocated prefill and decode, with arriving long prompts and latency constraints on ongoing decode requests.
- Small token chunks activate many experts, making expert-weight reads expensive; smaller decode batches make this worth investigating.

Pausing and resuming by layer is not tied to one GPU, but the reported gains depend on MoE access patterns. The authors observed regressions for dense Qwen3-8B. Relaxing inter-token latency constraints and allowing larger token chunks also narrows the advantage. A lone request in pure decode, with no arriving prefill, has no extra prefill weight loads for this mechanism to eliminate.

## Implementation pointers

The [public implementation](https://github.com/scale-snu/layered-prefill/tree/053f80e5201a7c0ab56e468e3d578907a2ca9cc3) uses Nano-vLLM and provides scheduling and benchmark commands.

vLLM expresses scheduled work as per-request token counts in [scheduler output](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/vllm/v1/core/sched/output.py). Its [Qwen3 MoE forward](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/vllm/model_executor/models/qwen3_moe.py) traverses the layers assigned to the current pipeline stage. Layer-group scheduling needs to pass per-request layer progress through these interfaces and retain the state needed to resume across iterations.

A port would need the following state and execution changes:

1. Track each prefill request's layer group and prompt/chunk range, distinguishing group completion from completion of all prefill work.
2. Execute a selected layer group and retain hidden states, required residuals, positions, and KV mappings across iterations. Produce the first token only after the final group completes.
3. Account for preemption, cancellation, batch regrouping, and CUDA Graph state and shape constraints. Intermediate state consumes memory; overly large layer groups can increase decode waiting time.

Compare token-chunk and layer-group scheduling on identical request traces. Measure TTFT, tail inter-token latency, peak memory, and expert-weight reads, holding model and sampling settings constant and checking output agreement. Measure end-to-end gains under mixed serving load.

## References

- [From Tokens to Layers: Redefining Stall-Free Scheduling for MoE Serving with Layered Prefill, v2](https://arxiv.org/html/2510.08055v2). Main experiments use two H100 80GB GPUs, NVLink, and TP2, with Qwen3-30B-A3B and GPT-OSS-20B on ShareGPT/arXiv serving workloads. Counting bytes loaded for expert weights, the authors report reductions of 12% and 39% for the two workload families.
- [Layered Prefill implementation and reproduction instructions](https://github.com/scale-snu/layered-prefill/tree/053f80e5201a7c0ab56e468e3d578907a2ca9cc3), including dependencies, a FlashAttention patch, serving, and measurement commands.
