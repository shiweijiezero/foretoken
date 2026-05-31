# 04 — Workloads & evaluation methodology

The eval must be trustworthy from the start (no rework). Core framing: the win is NOT "does
caching help" (LRU APC already wins that) but "does a **value/reuse-aware** policy beat **LRU**
on **long-lived, time-separated reuse under memory pressure**." That dictates everything:
workloads need a **time dimension** + **heavy-tailed hot-block popularity**, the objective is
**goodput under joint SLO** (not throughput, not instantaneous hit-rate), and we bound against
**Belady-optimal**.

## Traces (priority order)
| trace | reuse structure | time gaps | content | use |
|---|---|---|---|---|
| **Mooncake** (kvcache-ai/Mooncake, FAST'25) | **yes — `hash_ids`** (block=512 tok) | yes (1h, ms) | no | **primary** reuse + arrival driver |
| vLLM×Mooncake agentic (Codex/SWE) | yes | **yes (turn idle 5.2s med / 81.4s P99)** | no | the time-dimension model |
| Azure 2023/2024 (AzurePublicDataset) | no | yes (arrivals) | no | arrival + length heavy-tails only |
| BurstGPT | no | yes (bursty) | no | high-concurrency / burst |
| ShareGPT / LMSYS-Chat-1M | turn structure | synth | **yes** | genuine byte-identical multi-turn prefixes |

Mooncake schema: `{timestamp_ms, input_length, output_length, hash_ids[]}`; shared leading
hash_ids = shared prefix (block 512 tok). Real Kimi ceiling: **only ~50% KV reusable even at
∞ capacity**; hit 30%→50% as capacity 1k→50k blocks. **Target the 30–50% regime, not the 90%+
of degenerate microbenchmarks.** >50% of blocks never reused; a few hit 10,000s (heavy tail).

## Benchmark construction
- **Honor real timestamps** (no time compression) — preserves the inter-arrival gaps that make
  eviction decisions matter. vLLM `bench serve --dataset-name timed_trace --timed-trace-chunk-hash-size 512`.
- **Memory pressure mandatory**: size GPU KV (and tiers) so **working set > capacity**, else no
  eviction happens and the policy is untested.
- **Time dimension** (the crux — without it LRU ≈ value-aware): loop Mooncake K× over a multi-hour
  span with a carried-over hot hash_id set (carry fraction from the offline reuse-distance tail);
  for multi-turn, inject inter-turn idle (5.2s med / 81.4s P99) so the cache must **survive past
  where LRU would evict under load**.
- Synthetic generators (`generated-shared-prefix`, `prefix_repetition`) = **sanity microbenchmarks
  only** (no time dimension, no heavy tail) — never the headline.

## Metrics
- **Primary: goodput** = max SLO-meeting req/s under **joint TTFT_P90 ≤ a× + TPOT_P90 ≤ b×**
  baseline (Mooncake uses 10× / 5×), ≥90% attainment; **only fully-completed requests count**.
- **goodput-per-GPU-byte** — the cache-management efficiency metric (same goodput at less memory,
  or more goodput at equal memory); isolates policy from hardware.
- **Value-of-long-lived-reuse** (instantaneous hit-rate ≠ value): **hit-rate-vs-capacity curve**;
  **reuse-distance-stratified hit-rate** (the win must concentrate in long-distance buckets ≥min —
  the direct signature LRU loses those first); **evicted-then-immediately-needed count** (KVFlow's
  failure mode); **recompute-tokens-saved**; **fraction of LRU→Belady gap closed** (offline oracle).
- **Latency**: TTFT/TPOT **P50/P99 distributions** separately (cache touches TTFT; a TPOT move is
  suspicious → investigate confound). Token-level hit-rate (not request-level).
- ≥3 seeds + 95% CIs; identical trace/seed/capacity/model/parallelism/CUDA-graph/warmup/SLO across
  arms — only the policy differs.

## Baselines (equal total cache-byte budget, same everything else)
no-reuse (APC off) → **stock vLLM APC (LRU)** *(primary to beat)* → vLLM APC + CPUOffloadingManager
→ LMCache → SGLang HiCache (cross-system reference, not a controlled ablation). Disable/​hold-fixed
orthogonal accelerators (spec decoding) so the delta is attributable to cache management.

## Pitfalls → fixes (these caused rework before)
1. **Random-token degeneracy** (random tokens never share prefixes → fake/zero hits; also distorts
   MoE routing) → drive reuse from **real trace hash_ids** or **real content** (ShareGPT).
2. **Prefill-vs-decode confound** (cache touches TTFT only) → report TTFT/TPOT separately.
3. **Batch/concurrency effects** → fix the arrival process; sweep concurrency as an explicit axis;
   report goodput not throughput.
4. **Warm-vs-cold cheating** → discard fixed warmup, measure steady-state, same start state via
   `/reset_prefix_cache`, report the condition.
5. **CUDA-graph vs eager** → same graph setting for all arms (never eager-baseline vs graph-treatment).
6. **Throughput when goodput is the objective** → goodput-under-SLO is the headline.
7. **Goodput gaming** (abandon-late / delay-tokens-to-game-TBT) → commitment-relative deadlines,
   count relaunched/abandoned as load, forbid token-delaying (arXiv:2410.14257).
8. **Single-region ≠ multi-node** → for multi-node, fix instance count + report routing policy.
9. **Infinite-capacity ceiling confusion** → always state capacity; show hit-vs-capacity curve;
   compare to Belady at each capacity.
10. **Replay infidelity (silent killer)** → see gate-zero below.

## Two build-time validation gates (do BEFORE any policy experiment)
- **Gate-zero (replay correctness)**: replay Mooncake through **stock vLLM APC** and confirm the
  **measured token-level hit-rate ≈ the hit-rate computed offline from hash_ids** (within a few %).
  If they diverge, the hash_id→byte-identical-token reconstruction is broken and every number is
  invalid. (Verify the driver's hash→token determinism in code — do not assume; flagged for AIPerf
  and the vLLM Mooncake-store connector path.)
- **Belady oracle**: compute the clairvoyant-optimal hit-rate from the trace (all future reuses
  known offline); report value-aware vs LRU as **fraction of the LRU→Belady gap closed**.

## The falsifiable MVP gate (commit before running)
> A value-aware OffloadingManager beats **stock vLLM APC (LRU)** at **equal total cache-byte
> budget** by **≥15% goodput** (SLO-meeting req/s under joint TTFT_P90≤10× & TPOT_P90≤5× baseline,
> ≥90% attainment) on the **long-lived Mooncake replay** (real-time timestamps, working-set > GPU
> capacity, 30–50% structural hit ceiling), with the gain **concentrated in long reuse-distance
> buckets (≥1 min)**, reproducible across **≥3 seeds with non-overlapping 95% CIs**, **closing
> ≥40% of the LRU→Belady hit-rate gap**, and **no TPOT regression**.

Secondary: ≥10% goodput on the agentic multi-turn trace; the goodput-per-GPU-byte memory-efficiency
claim at matched goodput; **no win on the synthetic microbenchmarks is a red flag** (means we only
help where LRU already wins). This gate can't be passed by degeneracy, warm/cold cheating,
prefill/decode confounds, infinite-capacity ceilings, or goodput gaming.

## Sources
Mooncake 2407.00079 + repo traces; vLLM×Mooncake agentic blog (2026-05-06); Azure (Splitwise
2311.18677 / DynamoLLM 2408.00741); BurstGPT 2401.17644; LMSYS-Chat-1M 2309.11998; AttentionStore
2403.19708; KVFlow 2507.07400; DistServe 2401.09670; Revisiting-SLO/Goodput 2410.14257; SGLang
HiCache (LMSYS 2025-09-10); LMCache 2510.09665; vLLM APC + bench serve docs; NVIDIA AIPerf / SPEED-Bench.
