# 02 — vLLM v0.22.0 KV-cache hook-point map

Where to implement KV optimizations in vLLM, grounded in the v0.22.0 source (file:line).
**Verdict: the MVP and most of the vision are out-of-tree plugins — only two things need a
core fork, both deferred.**

## Internals (the facts)
- **Block management**: `KVCacheBlock` (`kv_cache_utils.py:116`), `BlockPool` (`block_pool.py:130`)
  owns `blocks`, the `FreeKVCacheBlockQueue` (`kv_cache_utils.py:164` — intrusive DLL, **= LRU**
  victim order), and `cached_block_hash_to_block` (the APC map). Allocation `get_new_blocks`
  (`block_pool.py:305`) pops LRU + may evict; free returns blocks reversed (tail evicts first).
- **Prefix caching (APC)**: block hash = parent_hash + own tokens + extra_keys
  (`kv_cache_utils.py:541`); **block-level (16 tok), full blocks only**; lookup = strict
  longest-prefix, **`break` on first miss** (`single_type_kv_cache_manager.py:495`) — the barrier
  to non-prefix reuse. Eviction = lazy LRU via the free queue (`_maybe_evict_cached_block`
  `block_pool.py:333`); **no value/cost/reuse signal**.
- **Scheduler**: `sched/scheduler.py:329`; KV availability gates `allocate_slots`; preemption
  FCFS pops the tail running request (`:481`), full recompute on resume. Prefix hits only reduce
  `num_new_tokens` — no "prefer high-hit request" logic.
- **MoE wide-EP**: KV partitioned **per DP rank** — each `DPEngineCoreProc` (`v1/engine/core.py:1651`,
  MoE-only) owns its own Scheduler+KVCacheManager+BlockPool (engine_id `_dp{rank}`). EP is
  orthogonal to KV. **No global KV view.**
- **Determinism**: already shipped — `VLLM_BATCH_INVARIANT` + attention `num_splits=1`
  (`flash_attn.py:1048`). Parity with SGLang; **no work needed**.

## Extension surfaces (CLEAN — out-of-tree, no fork)
| surface | how to load | what it controls | citation |
|---|---|---|---|
| **`OffloadingManager` + `OffloadingSpec`** | `spec_module_path` | offload-tier **admission** (`prepare_store`, refuse/filter keys), **eviction** (custom `CachePolicy.evict`), tier mgmt; `ReqContext` carries per-req signal | `v1/kv_offload/base.py:111`; ref `cpu/manager.py` (`store_threshold` reuse-count admission `:145`), `cpu/policies/base.py:35` (LRU/ARC) |
| **`Scheduler` via `scheduler_cls`** | qualname in `scheduler_config` | admission/ordering, reuse-vs-recompute gate | `config/scheduler.py:127`; `sched/interface.py:36` |
| **KV connector** | `kv_connector_module_path`; `MultiConnector` composes | `bind_gpu_block_pool` → GPU BlockPool handle (ref/evict/iterate); worker `save_kv_layer` → read/write any KV slot; `get_num_new_matched_tokens` → inject external prefix hits / decline loads | `kv_transfer/kv_connector/v1/base.py:171,432,354`; `factory.py` |

## The two CORE FORKS (deferred)
1. **Value-aware GPU-tier eviction** — victim hardwired to `FreeKVCacheBlockQueue` recency
   (`kv_cache_utils.py:164` + `block_pool.py:305-365`), **no plugin seam**. → MVP leaves GPU LRU;
   all value-awareness in the offload tier.
2. **Token-level prefix caching** — hash/lookup/alloc block-quantized end-to-end (gap #4). Deep
   fork. (Token-level *reuse*, not caching, is reachable via the worker-side blend.)

## The one hard structural constraint
The scheduler↔connector external-hit channel is a **scalar contiguous-prefix token count**
(`kv_cache_manager.py:341`). ⟹ **non-prefix reuse cannot go through the scheduler path** — it
must be worker-side per-layer `save_kv_layer` blend (PIECEWISE cudagraph; currently mutually
exclusive with vLLM APC). LMCache already ships exactly this as a connector
(`lmcache_integration/vllm_v1_adapter.py:859`). ⟹ lifecycle (MVP) is the right *first* target
(clean offload plugin); non-prefix is a later phase with known cost.

## Hook-point map (per gap → surface → verdict)
| gap | surface (file:line) | verdict |
|---|---|---|
| (a) value-aware long-lived lifecycle | custom `OffloadingManager.prepare_store`/`evict` + custom `CachePolicy`; + `Scheduler` ordering | **CLEAN plugin** (offload tier). GPU-tier eviction = deferred fork. |
| (b) non-prefix / CacheBlend | worker `save_kv_layer` blend (custom connector) | **CLEAN connector** (PIECEWISE cudagraph cost; LMCache-proven) |
| (c) reuse-vs-recompute cost model | connector `get_num_new_matched_tokens` (return 0 to decline) + custom `Scheduler` | **CLEAN** (cost decision must be idempotent/side-effect-free) |
| (d) finer-than-block reuse | `block_size` knob (cheap) / worker slot-writes (reuse) | **CLEAN** for reuse; **FORK** for token-level *caching* |
| (e) MoE wide-EP / cross-rank | connector + shared content-hash store (no single block mgr) | **CLEAN connector**; reconcile N per-DP-rank pools |

## Bottom line
The **OffloadingManager/Spec + `scheduler_cls` + KV connector** trio is rich enough to build
best-in-class KV management WITHOUT forking vLLM core. Keep GPU eviction LRU; put value/cost/
reuse intelligence in the offload tier + a custom scheduler; reserve the two core forks for
later, isolated, and only if a measured offload-tier ceiling binds.

*Source: file:line citations verified against `draft_router/vllm/` @ tag v0.22.0 (the prior phase's fork).*
