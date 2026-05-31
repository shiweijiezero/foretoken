# KVOS *(working name; TBD)*

**Best-in-class KV-cache management for vLLM** — take one engine (vLLM) and optimize its
KV-cache management *to the extreme* for large Dense/MoE models, multi-node, high
concurrency, and **long-lived KV reuse**. Built as **out-of-tree vLLM plugins** (no core
fork for the MVP). Speculative decoding is deferred to a later module.

> **Status: research / design phase.** Design-first, rigorous, source-grounded. **No code
> yet** — we finalize positioning + the architecture against vLLM's real internals before
> implementing, to avoid rework.

## Thesis
The LLM-inference **engine** layer (vLLM/SGLang/TRT-LLM) and the **control-plane** layer
(Dynamo/llm-d/AIBrix/KServe) are converged and crowded — building either is negative-EV.
The KV-cache **mechanics** (paged/radix cache, tiered offload, KV-aware routing, P/D
disaggregation, transfer fabrics) are mature. The genuinely un-won battleground is the
**policy / optimization brain** over KV: every system governs KV with disjoint, heuristic,
recency-based policies (LRU/LFU/S3FIFO). We build the missing brain — **treat KV as a
first-class, value-priced resource, governed by one objective: goodput-per-GPU-byte-second**
— inside vLLM, for the long-lived (hours/days) reuse that agentic/RAG/multi-turn serving needs.

## Why vLLM, why KV
vLLM is the engine to bet on (PyTorch Foundation, de-facto substrate, best plugin surface).
Its KV reuse is the headroom: block-level APC, prefix-only, plain-LRU, **no value-/SLO-aware
lifecycle, no hour/day persistence model** — behind SGLang's token-level radix. Making vLLM's
KV best-in-class is high-impact + clear headroom + exactly the long-lived-reuse target.

## Approach (out-of-tree plugins; no core fork for the MVP)
- **Custom `OffloadingManager`/`OffloadingSpec`** — the value-aware long-lived lifecycle
  (admission/eviction/TTL by `P(reuse)×recompute_cost×SLO_value`). Long-lived state lives in
  the offload tier (GPU stays LRU — GPU blocks are reclaimed under pressure regardless).
- **Custom `Scheduler` (`scheduler_cls`)** — value-aware admission/ordering + reuse-vs-recompute.
- **Custom KV connector** — cost model; later: non-prefix CacheBlend; cross-rank coordination.
- Two core forks (GPU-tier eviction, token-level caching) are **deferred** until plugins prove
  insufficient.

## MVP & gate
A value-aware `OffloadingManager` that, on a real long-session / agentic / RAG trace, beats
**stock vLLM** (LRU APC + default offload) on **cache-hit-rate** and **goodput per GPU-byte**,
determinism preserved. If we can't beat stock vLLM here, the thesis fails — first falsifiable gate.

## Non-goals
No from-scratch engine. No cross-engine layer (single-engine vLLM). No general control plane.
No new transfer fabric/offload tier (mount NIXL/Mooncake/LMCache). Determinism reused
(`VLLM_BATCH_INVARIANT`), not rebuilt. No version-naming.

## Docs
- [`DESIGN.md`](DESIGN.md) — architecture (grounded in vLLM v0.22.0) + roadmap (P0–P4).
- [`docs/01-market-landscape.md`](docs/01-market-landscape.md) — 3-front market survey synthesis.
- [`docs/02-vllm-kv-hookpoints.md`](docs/02-vllm-kv-hookpoints.md) — vLLM KV hook-point map (file:line).
- `docs/03-value-function.md` — value-aware eviction theory + the value-function design *(in progress)*.
- `docs/04-eval-methodology.md` — workloads + benchmark/eval methodology *(in progress)*.
- `docs/05-open-questions.md` — design dives before any code *(pending)*.
