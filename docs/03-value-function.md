# 03 — Value-function design + the crowded frontier (positioning reality-check)

Scope: cross-request **prefix-cache block** eviction/admission (the radix/block layer), NOT
within-sequence attention-sparsity token dropping (H2O/SnapKV/Ada-KV — different problem, different
correctness). Value target: `value(block) = P(reuse) × recompute_cost × SLO_value`.

> **⚠️ Reality-check (the load-bearing finding): this space is already crowded as of late-2025/2026.**
> A naive "value-aware KV eviction beats LRU" is **NOT defensible** — it's incrementally ahead of
> GDSF (1998) / LHD (NSDI'18), and the LLM-specific instances already exist. The defensible novelty
> is narrow and the hard part is the `P(reuse)` estimator. See §3–§6.

## 1. Classical theory → adopt the LHD form
| policy | value function | note |
|---|---|---|
| Belady / **PFOO** (PoMACS'18) | farthest-future / variable-size offline optimal | **the right offline oracle** (KV blocks are variable-length; use PFOO, not vanilla Belady) |
| GDS / **GDSF** (USITS'97/'98) | `Clock + Freq·cost/Size` | heuristic ancestor: cost+size+freq, recency via aging; **no residency denominator** |
| **LHD** (NSDI'18) | **`P(hit) / (size × E[residency])`** (conditional on age + class) | the principled "expected value per byte-second"; natively takes a cost/value term — **the canonical target** |
| TinyLFU / W-TinyLFU | admit iff CMS-freq(candidate) > freq(victim) | the admission primitive |
| S3FIFO (SOSP'23) / SIEVE (NSDI'24) | 1-bit freq + quick-demotion | O(1), Zipf-robust; the robust-fallback to blend with |

## 2. Learned caching → hot-path analytic + background learning
LRB (NSDI'20, mimic Belady) is too heavy per-block; **GL-Cache (FAST'23): learn at GROUP level →
228× LRB throughput**. ⟹ engineering pattern: **hot path = closed-form density over cheap node
features + LHD's 64-object random-sample ranking** (no global sort, no per-block NN); **learning in a
background loop** (per-class distribution params, not per-access inference).

## 3. The crowded frontier (KV-specific, 2024–2026) — what already exists
- **SAECache** (arXiv 2605.18825, 2026): **literally an LHD hit-density-per-time policy for KV prefix
  blocks** — `P(b)=α_q·w_τ(b)·p_q(b)/Δt_b` with learned token-type weights (system-prompt 92% reuse vs
  CoT 2%) + log-normal survival. **P(reuse)-only** (no cost/SLO); regresses 12–34% on single-turn.
- **LPC** (NeurIPS'25): learned conversation-continuation P(reuse); 18–47% smaller cache vs LRU. P(reuse)-only.
- **vLLM T-LRU RFC #37823 (OPEN, ref impl exists)** — *the key competitive signal*: vLLM community is
  **right now** building recompute-cost + TTFT-SLO-aware prefix eviction (`B=max(0,H+Q̂−ξ)`). Cost+SLO+
  crude-reuse, but per-conversation, not per-byte density; Q̂ is heuristic not calibrated P(reuse).
- **TensorRT-LLM** ships priority+retention KV eviction (SLO as a *manual knob*).
- OrbitFlow (per-request ILP layer-retention, not per-byte), TokenCake (agent criticality), EVICPRESS
  (unified utility but for compression), KVFlow (workflow steps-to-execution, rule-based).

## 4. Shipping policies — the precise gap
All rank on **recency ± frequency ± a manual priority**: vLLM = pure LRU; SGLang radix = LRU/LFU/FIFO/
MRU/Priority (`len(node.key)` **available but unused**; `node.priority` is an external int hook);
HiCache = reuse-count write-through admission (TinyLFU-doorkeeper-like) but recency eviction;
LMCache = LRU/LFU/FIFO(+ARC); AIBrix = S3FIFO; Mooncake = LRU/LFU/LengthAware. **None compute
value-per-byte; size unused where available; recompute cost never modeled; SLO only manual; P(reuse)
at best 1-bit/2-hit.**

## 5. The defensible novelty (state it this narrowly)
> **A single per-byte expected-goodput density that fuses calibrated `P(reuse)` (persistence-aware) ×
> physically-grounded `recompute-vs-reload cost` × `SLO/tenant value` into ONE ranking, governing
> BOTH eviction AND cross-tier admission — and reducing provably to LHD, GDSF, and T-LRU as special
> cases.** No prior fuses all three: SAECache/LPC omit cost+SLO (and regress on single-turn);
> T-LRU/TRT-LLM omit calibrated reuse + per-byte density; OrbitFlow is per-request not per-byte;
> EVICPRESS's utility is for compression. The unification + the subsumption proof + tier-aware
> admission are the contribution; the per-byte-value idea itself is **not** novel.

## 6. The canonical objective
**`Value(b) = [ P(reuse|Δt,class) × recompute_cost(b) × SLO_value(b) ] / [ size(b) × E[residency|class] ]`**
— evict ascending; admit to a tier iff Value exceeds the marginal value of displaced bytes (TinyLFU-style,
value-generalized). Reduces to **LHD** (cost=SLO=1), **GDSF** (drop residency denom), **T-LRU** (class=conv,
binarize SLO at TTFT budget). The `E[residency]` denominator (LHD's distinguisher, GDSF lacks it) stops
large/long blocks squatting — important since KV blocks are large + variable-length.
- `recompute_cost = min(prefix_len·c_recompute, bytes/B_PCIe + lat)` — the **PCIe↔HBM tradeoff** (reload
  2–10 ms; offload only helps below a critical cached/prefill ratio), **tier- and load-dependent**.
- `SLO_value = w(tenant_priority)·g(deadline_slack)`, weighted by max over the requests a block serves.

## 7. Hardest sub-problem (confirmed) + mitigations
The **`P(reuse)` estimator under hour/day persistence** (shared system prompts, RAG chunks, returning
sessions) — SAECache only models short multi-turn and regresses on single-turn; no calibrated
nonstationary long-tail model exists. Danger: miscalibration → pin cold bytes OR evict soon-reused
shared prefixes (recompute storm). Build in from day one: (1) **competitive safety** — blend learned
density with S3FIFO/LRU so worst case stays within the ML-augmented `min(2+4√(η/OPT),4H(k))` bound;
(2) **per-class survival fits with uncertainty** (shrink to recency when few samples); (3) **evaluate
vs PFOO** (variable-size optimal), not vanilla Belady.

## 8. Implication for the MVP gate (sharpens `04`)
LRU is now a **weak** baseline. The honest gate must beat LRU **and** approach/beat the real
competitors (**T-LRU, SAECache**) and close a large fraction of the **LRU→PFOO** gap — with the win
concentrated in the long-reuse-distance buckets where calibrated long-horizon `P(reuse)` is the
differentiator. (vLLM integration seams for this are in `02-vllm-kv-hookpoints.md`; this doc is the
engine-agnostic value-function theory.)

## Sources
LHD NSDI'18 · GDSF USITS'97 · TinyLFU 1512.00727 · S3FIFO SOSP'23 · SIEVE NSDI'24 · LRB NSDI'20 ·
GL-Cache FAST'23 · PFOO PoMACS'18 · ML-augmented caching 1910.12172 · **SAECache 2605.18825** ·
**LPC NeurIPS'25** · **vLLM T-LRU RFC #37823** · TRT-LLM priority eviction (NVIDIA) · KVFlow 2507.07400 ·
OrbitFlow 2601.10729 · TokenCake 2510.18586 · EVICPRESS 2512.14946 · SmartSpec 2406.14066 ·
KV-offload bottleneck 2601.19910.
