# KVOS — best-in-class KV-cache management for vLLM
*(working name; TBD)*

Take **one engine — vLLM — and optimize its KV-cache management to the extreme** for large
Dense/MoE, multi-node, high concurrency, and **long-lived KV reuse**. Built as **out-of-tree
vLLM plugins** (custom OffloadingManager + Scheduler + KV connector) — **no core fork for the
MVP** — with two well-isolated core forks reserved for later. Speculative decoding deferred
(prior cross_vocab work preserved & resumable). Founded on a 3-front mid-2026 market survey +
a file:line hook-point map of vLLM v0.22.0.

---

## 1. Why vLLM, why KV
vLLM = the engine to bet on (PyTorch-Foundation, Apache-2.0, de-facto substrate, best plugin
surface, team reliability). Its KV reuse is the headroom: **block-level APC (16-tok hashing),
prefix-only (RAG non-prefix misses), plain-LRU eviction, no value-/SLO-aware lifecycle, no
hour/day persistence model** — behind SGLang's token-level radix. Making vLLM's KV best-in-class
is high-impact (most users) + clear headroom + exactly the "长时间复用 / large-MoE / multi-node"
target.

## 2. The gaps we close, inside vLLM (ranked)
1. **Value-aware long-lived cache lifecycle** *(MVP)* — admission/eviction/TTL by
   `P(future reuse) × recompute_cost × SLO_value`, maximizing **goodput-per-GPU-byte-second**,
   across GPU→CPU→NVMe→remote tiers, for hour/day session/agentic/RAG persistence.
2. **Non-prefix / partial reuse** (CacheBlend-style) — RAG chunks that aren't a shared prefix.
3. **Online reuse-vs-recompute cost model** — per block/tier vs the ~50–100× PCIe↔HBM cliff.
4. **Finer-than-block reuse** — toward token-level (vLLM wastes partial-block matches).
5. **Large-MoE multi-node KV** — value-aware KV under attention-DP + wide-EP; long-context wall.

## 3. Architecture — grounded in vLLM v0.22.0 internals
**Verdict from the hook-point map: the MVP and most of the vision are buildable as out-of-tree
plugins; only two things require a core fork (both deferred).**

### 3.1 Primary integration surfaces (CLEAN — no core fork, load out-of-tree)
- **Custom `OffloadingManager` + `OffloadingSpec`** → registered via `spec_module_path`
  (`v1/kv_offload/factory.py`; ABCs at `v1/kv_offload/base.py:111-220`). **This is where the
  value-aware long-lived lifecycle lives.** `prepare_store(keys, ReqContext)` = admission (the
  vLLM reference `CPUOffloadingManager` already gates by a reuse-count `store_threshold`,
  `cpu/manager.py:145` — we generalize it to `P(reuse)×recompute_cost×SLO_value`); a custom
  `CachePolicy.evict` (`cpu/policies/base.py:35-83`; ships LRU/ARC) = value-aware eviction;
  `ReqContext(req_id, kv_transfer_params)` carries per-request value signal. A `TieringOffloadingSpec`
  already exists for GPU→CPU→disk. **Key insight: hour/day persistence is NOT a GPU concept —
  GPU blocks are reclaimed under pressure regardless; long-lived state must live in this tier.**
- **Custom `Scheduler` via `scheduler_cls`** (`config/scheduler.py:127` → `resolve_obj_by_qualname`;
  `SchedulerInterface` at `sched/interface.py:36`; vLLM ships `AsyncScheduler` this way). Subclass
  `Scheduler.schedule()` (`sched/scheduler.py:329`) for **value-aware admission/ordering** + the
  **reuse-vs-recompute gate**. (Caveat: interface marked non-public → version-fragile, but no fork.)
- **Custom KV connector** (`kv_transfer/kv_connector/v1/base.py:171`; out-of-tree via
  `kv_connector_module_path`, composes with others through `MultiConnector`). `bind_gpu_block_pool`
  (`base.py:432`) hands us the GPU `BlockPool` (ref-count/evict/iterate). Worker-side per-layer
  `save_kv_layer` (`base.py:354-401`, via `@maybe_transfer_kv_layer`) lets us read/write arbitrary
  KV slots → the path for **(b) non-prefix CacheBlend** (LMCache already ships exactly this as a
  connector) and **(c) the cost model** (`get_num_new_matched_tokens` returns 0 to decline a load).

### 3.2 The two CORE FORKS — deferred, well-isolated, only if plugins prove insufficient
- **Value-aware *GPU-tier* eviction**: the victim is hardwired to `FreeKVCacheBlockQueue` recency
  (`kv_cache_utils.py:164-372`) + `block_pool.py:305-365` — no plugin seam. **MVP leaves GPU
  eviction as LRU** and puts all value-awareness in the offload tier; fork only later if needed.
- **True token-level prefix caching**: hash/lookup/allocation is block-quantized end-to-end
  (gap #4) — a deep fork. Token-level *reuse* (not caching) is reachable via the worker-side blend.

### 3.3 The one hard structural constraint (drives the design)
The scheduler↔connector external-hit channel is a **scalar contiguous-prefix token count**
(`kv_cache_manager.py:341-342`). So **non-prefix reuse cannot go through the scheduler path** — it
must be a worker-side per-layer blend (PIECEWISE cudagraph, currently mutually exclusive with vLLM
APC). This makes (a) lifecycle (MVP) the right *first* target (clean offload-tier plugin) and (b)
non-prefix a *second* phase with known cost.

### 3.4 Free wins / non-issues
- **Determinism already solved**: `VLLM_BATCH_INVARIANT` + attention `num_splits=1`
  (`flash_attn.py:1048`) = parity with SGLang. Keep as a first-class test invariant; no build work.
- **MoE wide-EP**: KV is **partitioned per DP rank** (each `DPEngineCoreProc`, `v1/engine/core.py:1651`,
  owns its own Scheduler+KVCacheManager+BlockPool; engine_id `_dp{rank}`). EP is orthogonal to KV. A
  cross-rank value-aware cache = our connector + a shared content-hash store (Phase 3).

## 4. MVP — first falsifiable result
A **custom value-aware `OffloadingManager`** (value model = `P(reuse)×recompute_cost×SLO_value`;
admission via `prepare_store`, eviction via a custom `CachePolicy`, TTL for hour/day persistence;
GPU→CPU→NVMe tiers) — a pure out-of-tree plugin on vLLM's offloading subsystem. **Gate:** on a real
long-session / agentic / RAG trace (Mooncake trace + synthetic), beat **stock vLLM (LRU APC +
default `CPUOffloadingManager`)** on **cache-hit-rate** and **goodput (SLO-meeting req/s) per
GPU-byte** at equal hardware, determinism preserved. If we can't beat stock vLLM here, the thesis
fails — first gate.

## 5. Roadmap (phased; each phase has a falsifiable gate vs a baseline)
- **P0 — scaffold & baseline harness.** Repo + vLLM-plugin entry points + a benchmark harness
  (Mooncake trace + synthetic long-session/RAG) measuring hit-rate / goodput / GPU-byte /
  TTFT-TPOT, with **stock vLLM (LRU APC + CPUOffloadingManager)** as the baseline. Gate: reproduce
  stock numbers.
- **P1 — value-aware OffloadingManager (MVP).** The value model + admission + custom eviction +
  TTL. Gate: beat stock on the long-session/agentic/RAG trace.
- **P2 — reuse-vs-recompute cost model + value-aware Scheduler.** Online load-vs-recompute; admission
  ordering. Gate: goodput gain under high concurrency / KV pressure.
- **P3 — non-prefix reuse (CacheBlend connector)** + cross-rank shared-hash store for wide-EP MoE.
  Gate: RAG hit-rate beyond prefix-only; multi-instance reuse.
- **P4 (optional) — GPU-tier value-aware eviction core fork**, only if P1–P3 show the offload-tier
  ceiling binds. **Later — speculative decoding** re-enters as one more goodput lever in this loop.

## 6. Explicit non-goals
No from-scratch engine. No second engine / no cross-engine layer (single-engine vLLM). No general
control plane. No new transfer fabric/offload tier (mount NIXL/Mooncake/LMCache). No competing
routing API. Determinism is reused, not rebuilt. Spec decoding deferred.

## 7. Rigor commitments
Every decision cites vLLM v0.22.0 (file:line) + is benchmarked vs stock vLLM + the named incumbent
(LMCache/HiCache). Each component ships a falsifiable metric vs a baseline before "done".
Determinism/correctness = first-class test target. Plugin-first; any core fork is isolated +
justified by a measured offload-tier ceiling. Vendor multipliers = upper bounds; we measure ours.

## 8. Open design questions (dives before P1)
- The `P(future reuse)` estimator + per-request-class SLO_value model (the value function).
- Eviction/TTL data structures for hour/day persistence + overhead budget vs reuse-count + LRU/ARC.
- Threading the value signal through `ReqContext` (today only `req_id`+`kv_transfer_params`).
- Benchmark harness: exact traces (Mooncake) + baselines + metric definitions (goodput-per-byte).
- Upstream-contribution vs maintained-fork posture (plugins favor upstreamable; forks don't).

## 9. Status
Engine **vLLM locked** (2026-06-01). MVP = value-aware OffloadingManager. vLLM v0.22.0 fork + env
ready (editable, cu129). Hook-point map done (this doc §3). **Next: P0 scaffold + baseline harness.**
