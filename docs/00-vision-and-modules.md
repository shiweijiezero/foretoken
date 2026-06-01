# 00 — Vision & module map (master)

## Vision
**Industrial-grade deployment + inference** of large Dense/MoE models, multi-node, high
concurrency. We do NOT reinvent the mature layers — we **assemble best-of-breed existing
modules on vLLM and build only the differentiated ones**. The differentiated core is
**value-aware long-lived KV-cache management**; **MTP speculative decoding** is a key module;
the goodput-aware control loop ties them together.

**Reconciliation with the market** (`docs/01`): engine layer + control plane are crowded/
mature → USE them. The un-won gaps (KV value-policy, goodput-optimal spec control) → BUILD.
This map makes the build-vs-use call explicit, module by module, so each gets targeted research
and we never build what already exists.

## Legend
**Use** = adopt an existing tool as-is · **Extend** = configure/plug into an existing surface
(no fork) · **Build** = our differentiated code · **Fork** = modify engine core (last resort).
Status: ✅ researched · 🔬 needs targeted research · ⬜ not started. Phase = P0–P4 (`DESIGN.md §6`).

## A. Engine / execution (per-node compute) — USE vLLM
| # | module | decision | tool / how | status | phase |
|---|---|---|---|---|---|
| A1 | inference engine (model exec, attention, batching) | **Use** | vLLM v0.22.0 (SGLang optional ref) | ✅ | P0 |
| A2 | quantization (FP8/FP4/INT4/AWQ, KV-quant) | **Use** | vLLM | ✅ | P0 |
| A3 | parallelism TP/PP/EP/DP, attention-DP, wide-EP (MoE) | **Use** | vLLM | ✅ | P0 |
| A4 | CUDA graphs / torch.compile | **Use** | vLLM | ✅ | P0 |
| A5 | determinism / batch-invariance | **Use** | vLLM `VLLM_BATCH_INVARIANT` | ✅ | P0/test |

## B. KV-cache management — our DIFFERENTIATED core (`DESIGN.md`, `docs/02/03`)
| # | module | decision | tool / how | status | phase |
|---|---|---|---|---|---|
| B1 | paged/radix KV + prefix caching (APC) | **Use** | vLLM APC | ✅ | P0 |
| B2 | hierarchical offload GPU→CPU→NVMe→remote | **Extend** | vLLM `OffloadingSpec`/`TieringOffloadingSpec`; mount LMCache/Mooncake | ✅ | P1 |
| **B3** | **value-aware lifecycle: admission/eviction/TTL (goodput-per-byte)** | **Build** | custom `OffloadingManager` + `CachePolicy` (canonical density, `docs/03 §6`) | ✅ design | **P1 (MVP)** |
| **B4** | non-prefix / partial reuse (CacheBlend) | **Extend→Build** | custom KV connector, worker `save_kv_layer` blend (LMCache-proven) | ✅ | P3 |
| **B5** | online reuse-vs-recompute cost model | **Build** | connector `get_num_new_matched_tokens` + scheduler; PCIe↔HBM model | 🔬 | P2 |
| B6 | KV transfer fabric | **Use** | NIXL / Mooncake Transfer Engine | ✅ | P2/3 |
| B7 | cross-node / global KV (KV-aware routing, shared store) | **Extend** | connector + shared content-hash store; conform to Gateway API IGW | 🔬 | P3 |
| B8 | `P(reuse)` long-horizon estimator (the hard part) | **Build** | per-class survival + uncertainty + competitive-safety blend | 🔬 (hardest) | P1 |

## C. Speculative decoding — **MTP is a key module** (the user's emphasis)
| # | module | decision | tool / how | status | phase |
|---|---|---|---|---|---|
| **C1** | **MTP speculative decoding** (native multi-token-prediction heads, e.g. GLM-4.5 / DeepSeek MTP) | **Use→Extend** | vLLM `glm4_moe_mtp` / deepseek-MTP spec path; tune draft length, integrate with our scheduler/KV | 🔬 | P2/3 |
| C2 | goodput-aware spec control (adaptive draft length, SLO-aware) | **Build/research** | SmartSpec/SpecServe/AdaServe class — *not productized in OSS*; our control loop | 🔬 | P3 |
| C3 | cross-vocab speculative decoding (prior work) | **Defer (research ext.)** | preserved cross_vocab; resumable as a research extension | ✅ (preserved) | later |

## D. Scheduling / serving control (per-instance)
| # | module | decision | tool / how | status | phase |
|---|---|---|---|---|---|
| D1 | continuous batching / chunked prefill | **Use** | vLLM | ✅ | P0 |
| D2 | scheduler (admission, ordering, preemption) | **Extend** | custom `scheduler_cls` (value-/goodput-aware) | ✅ design | P2 |
| D3 | P/D disaggregation | **Use** | vLLM + Mooncake/NIXL connectors | ✅ | P3 |
| D4 | SLO / goodput-aware scheduling | **Build/research** | goodput objective (DistServe/Mooncake def) in D2 | 🔬 | P2/3 |

## E. Orchestration / control plane (multi-instance, fleet) — USE, don't build #5
| # | module | decision | tool / how | status | phase |
|---|---|---|---|---|---|
| E1 | KV-aware router | **Use/Extend** | Gateway API Inference Extension (conform); or thin layer feeding B7 | 🔬 | P3+ |
| E2 | autoscaler (SLO/KV-util aware) | **Use** | Dynamo Planner / llm-d / AIBrix | ✅ | later |
| E3 | multi-model / multi-tenant | **Use/Defer** | AIBrix / KServe | ✅ | later |
| E4 | deployment (K8s, Helm, serving API) | **Use** | vLLM production-stack / KServe / llm-d; OpenAI-compatible front door | ✅ | later |

## F. Observability / correctness
| # | module | decision | tool / how | status | phase |
|---|---|---|---|---|---|
| F1 | metrics (TTFT/TPOT/goodput/KV-hit/cache) | **Extend** | vLLM Prometheus + our KV/goodput metrics | 🔬 | P0 |
| F2 | determinism / reproducibility | **Use** | vLLM batch-invariant; first-class test | ✅ | P0 |
| F3 | tracing / debugging (per-request KV-hit, route, accept) | **Build** | serving-native tracing (a known OSS gap) | 🔬 | later |

## G. Benchmark / eval harness — BUILD (`docs/04`)
| # | module | decision | tool / how | status | phase |
|---|---|---|---|---|---|
| G1 | trace replay (Mooncake) + long-lived workloads | **Build** | vLLM `bench serve --dataset-name timed_trace` + our long-lived constructor | ✅ design | P0 |
| G2 | baselines + metrics + PFOO oracle + gate-zero | **Build** | stock LRU / T-LRU / SAECache / LMCache; goodput-per-byte; replay-correctness gate | ✅ design | P0 |

## What we BUILD (the differentiated set) vs USE
- **Build (our IP):** B3 value-aware lifecycle (MVP) · B5 reuse-vs-recompute · B8 `P(reuse)` estimator ·
  B4 non-prefix reuse · D4 goodput scheduling · C2 goodput-aware spec control · G1/G2 eval harness · F3 tracing.
- **Use/Extend (mature):** the entire engine (A*), KV mechanics (B1/B2/B6/B7), MTP (C1), batching/P-D
  (D1/D3), the whole control plane + deployment (E*), base metrics/determinism (F1/F2).
- **Defer:** C3 cross-vocab; E2–E4 fleet; F3 tracing — after the differentiated core proves out.

## Cross-module constraints (found in research — must honor)
- **MTP draft KV is ephemeral** (`docs/05`): the drafter's `kv_cache_gid` writes KV for rejected drafts
  that is discarded → **B2/B3 must tag draft KV non-offloadable / non-cacheable** (explicit tag, not
  automatic). A C1↔B2/B3 contract.
- **Dynamic spec length (C2) must stay within captured cudagraph buckets**, else eager-fallback erases the gain.
- **MoE KV is partitioned per DP rank** (`docs/02`) → any cross-rank/global policy (B7) = a connector +
  shared content-hash store, not a single block manager.

## Targeted research queue (per 🔬 module, before its phase)
- ✅ **B8 `P(reuse)` estimator** → `docs/06` (factorized popularity×survival; token-type-only MVP; competitive-safety blend).
- ✅ **C1 MTP** → `docs/05` (C1 = USE vLLM native MTP; **C2 = BUILD** the goodput-aware MTP controller — the open slot).
- 🔬 **B5 reuse-vs-recompute** — the PCIe↔HBM cost model + tier-load signal.
- 🔬 **D4/C2 goodput control** — SmartSpec/Cascade/AdaServe goodput objective → our scheduler + the MTP controller.
- 🔬 **B7/E1 KV-aware routing** — conform to Gateway API IGW; the shared-hash store for cross-rank.
- 🔬 **F1 metrics** — the KV/goodput telemetry surface.

> Each 🔬 row gets a targeted research note (a `docs/` dive) + a build-vs-use confirmation before its
> phase starts. Still **design/research only — no code** until the queue above is sufficiently covered.
