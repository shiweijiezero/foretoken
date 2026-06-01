# lm_db — best-in-class KV-cache management for vLLM
*(working name; TBD)*

Take **one engine — vLLM — and optimize its KV-cache management to the extreme** for large
Dense/MoE, multi-node, high concurrency, and **long-lived KV reuse**. Built as **out-of-tree
vLLM plugins** (no core fork for the MVP), with two well-isolated core forks reserved for later.
Speculative decoding deferred (prior cross_vocab work preserved & resumable). Founded on a
3-front market survey + a file:line hook-point map of vLLM v0.22.0 + a value-function /
eval-methodology deep dive (`docs/01`–`04`).

> **Status: research / design phase. No code yet** — finalize positioning + architecture
> against vLLM's real internals before implementing, to avoid rework and version-churn.

## 1. Why vLLM, why KV
vLLM = the engine to bet on (PyTorch Foundation, Apache-2.0, de-facto substrate, best plugin
surface, team reliability). Its KV reuse is the headroom: block-level APC, prefix-only, **plain
LRU**, no value-/SLO-aware lifecycle, no hour/day persistence — behind SGLang's token-level radix.
Making vLLM's KV best-in-class is high-impact + clear headroom + exactly the long-lived-reuse target.

## 2. The gaps we close, inside vLLM (ranked)
1. **Value-aware long-lived cache lifecycle** *(MVP)* — admission/eviction/TTL maximizing
   **goodput-per-GPU-byte-second**, across GPU→CPU→NVMe→remote tiers, for hour/day persistence.
2. **Non-prefix / partial reuse** (CacheBlend-style) — RAG chunks that aren't a shared prefix.
3. **Online reuse-vs-recompute cost model** — per block/tier vs the ~50–100× PCIe↔HBM cliff.
4. **Finer-than-block reuse** — toward token-level (vLLM wastes partial-block matches).
5. **Large-MoE multi-node KV** — value-aware KV under attention-DP + wide-EP; long-context wall.

## 3. Architecture — grounded in vLLM v0.22.0 (`docs/02`)
**The MVP and most of the vision are out-of-tree plugins; only two things need a core fork (deferred).**
- **CLEAN surfaces (no fork):** custom **`OffloadingManager`/`OffloadingSpec`** (`spec_module_path`)
  = the value-aware long-lived lifecycle (admission `prepare_store`, eviction via custom `CachePolicy`,
  tiering); custom **`Scheduler`** (`scheduler_cls`) = value-aware admission/ordering + reuse-vs-recompute
  gate; custom **KV connector** (`kv_connector_module_path`; `bind_gpu_block_pool` → GPU pool handle;
  worker `save_kv_layer` → write any KV slot) = cost model + later non-prefix CacheBlend + cross-rank.
- **Key insight:** hour/day persistence is NOT a GPU concept (GPU blocks are reclaimed under pressure
  regardless) → long-lived state lives in the offload tier; **GPU stays LRU**.
- **Deferred core forks:** (1) value-aware *GPU-tier* eviction (victim hardwired to the recency free
  queue, no seam); (2) token-level prefix caching (block-quantized). Only if the offload-tier ceiling binds.
- **Hard constraint:** scheduler↔connector external-hit channel is a scalar prefix count → non-prefix
  reuse must be worker-side blend (PIECEWISE cudagraph; LMCache-proven) → lifecycle (MVP) is the right
  first target, non-prefix is later.
- **Free:** determinism already shipped (`VLLM_BATCH_INVARIANT`) = SGLang parity. **MoE wide-EP:** KV
  partitioned per DP rank → cross-rank cache = connector + shared content-hash store.

## 4. Novelty & competitive safety (sharpened by `docs/03`)
**The space is already crowded (late-2025/2026).** "Value-aware KV eviction beats LRU" is NOT novel —
SAECache (arXiv 2605.18825) is an LHD hit-density policy for KV blocks; LPC (NeurIPS'25) learns reuse;
**the vLLM T-LRU RFC #37823 is actively building cost+SLO-aware prefix eviction**; TRT-LLM ships priority
eviction. **The only defensible novelty:** a *single per-byte expected-goodput density* fusing
**calibrated P(reuse) × recompute-vs-reload cost × SLO/tenant value** into one ranking for **both
eviction AND cross-tier admission**, reducing provably to **LHD/GDSF/T-LRU** as special cases. No prior
fuses all three. The genuinely hard, contributory part is the **long-horizon (hour/day) `P(reuse)`
estimator** (SAECache regresses on single-turn; no calibrated nonstationary long-tail model exists).
**Competitive safety is day-one:** blend the learned density with a robust policy (S3FIFO/LRU) so the
worst case stays bounded — never trust the estimator blindly.
Canonical objective: `Value(b) = P(reuse|Δt,class)·recompute_cost·SLO_value / (size·E[residency|class])`.

## 5. MVP & falsifiable gate (`docs/04`)
A custom value-aware **`OffloadingManager`** (the canonical objective; hot-path analytic + LHD
64-sample ranking + background per-class param learning; GPU→CPU→NVMe tiers) — a pure out-of-tree
plugin. **Gate:** on the **long-lived Mooncake replay** (real-time timestamps, working-set > GPU
capacity, 30–50% structural hit ceiling), beat **stock vLLM LRU APC by ≥15% goodput at equal
cache-byte budget**, gain concentrated in **long reuse-distance buckets (≥1 min)**, ≥3 seeds with
non-overlapping 95% CIs, **closing ≥40% of the LRU→PFOO gap**, no TPOT regression — **and
approach/beat the real competitors (T-LRU, SAECache)**, not just LRU. Determinism preserved.

## 6. Roadmap (each phase has a falsifiable gate vs a baseline)
- **P0 — scaffold + baseline harness.** Repo + vLLM-plugin entry points + Mooncake-replay harness;
  **gate-zero**: measured stock-APC hit-rate ≈ offline hash_id hit-rate (replay correctness) + PFOO oracle.
- **P1 — value-aware OffloadingManager (MVP).** Gate §5.
- **P2 — reuse-vs-recompute cost model + value-aware Scheduler** (goodput under concurrency/KV pressure).
- **P3 — non-prefix CacheBlend connector + cross-rank shared-hash store** (RAG beyond prefix; multi-instance).
- **P4 (optional) — GPU-tier value-aware eviction core fork**, only if the offload-tier ceiling binds.
- **Later — speculative decoding** re-enters as one more goodput lever in this loop.

## 7. Explicit non-goals
No from-scratch engine. No cross-engine layer (single-engine vLLM). No general control plane. No new
transfer fabric/offload tier (mount NIXL/Mooncake/LMCache). Determinism reused, not rebuilt. No
version-naming.

## 8. Rigor commitments
Every decision cites vLLM v0.22.0 (file:line) + a primary source, and is benchmarked vs stock vLLM +
the real competitors (T-LRU/SAECache/LMCache) + the PFOO offline optimum. Each component ships a
falsifiable metric before "done". Determinism/correctness = first-class test. Plugin-first; any core
fork is isolated + justified by a measured ceiling. Vendor multipliers = upper bounds; we measure ours.

## 9. Open design questions (dives before P1)
- The `P(reuse)` estimator + per-class survival fits + uncertainty (the hardest part); SLO_value model.
- Eviction/TTL data structures for hour/day persistence + overhead budget; the 64-sample ranking impl.
- Threading the value signal through `ReqContext` (today only `req_id`+`kv_transfer_params`).
- Benchmark harness specifics (Mooncake replay hash→token determinism — gate-zero); the exact baselines.
- Upstream-contribution vs maintained-fork posture (plugins favor upstreamable).

## 10. Status
Engine **vLLM locked** (2026-06-01). MVP = value-aware OffloadingManager. vLLM v0.22.0 fork + env ready.
Surveys + hook-point map + value-function/eval research done (`docs/01`–`04`). Repo:
github.com/shiweijiezero/kvos (private). **Next: still research — `docs/05` design dives; no code yet.**
