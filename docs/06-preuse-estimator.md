# 06 — `P(reuse)` long-horizon estimator (module B8, the hardest core)

Engine-agnostic design for the reuse-probability term of the value function (`docs/03 §6`).
Integration in vLLM is via our custom **`CachePolicy`/`OffloadingManager`** (`docs/02`), NOT
SGLang's `evict_policy.py` (the agent's SGLang anchors are reference examples of the same seam).

## The reframe (the one finding that defines B8)
**KV reuse is short-horizon in practice** — 80–90% of reuse is within ~10 min (consumer) / ~10 s
(business); SAECache inter-turn P50 ≈ 110 s (chat) / 8.5 s (agentic); cross-session reuse < 0.01%.
**The hour/day mass (shared system prompts, hot RAG chunks, returning sessions) is a thin but
high-value tail that no prior estimator is fit on** — it's below the noise floor of session-locality
models and governed by a *different* generative process (cross-request popularity, not within-session
continuation). **B8's contribution = model that tail; the binding constraint is SAFETY** — a
miscalibrated long-horizon score that pins dead blocks causes a recompute/I-O storm.

## Model it as TWO distributions (conflating them is the classic error)
`P(reuse within H) = P(ever-reused | class) · S_class(Δt ≤ H)`
1. **Popularity / ever-reuse** = zero-inflated Bernoulli (Mooncake: >50% blocks never reused; "in the
   wild": 10% of blocks → 77% of reuses). S3FIFO's one-hit-wonder quick-demotion handles this mass.
2. **Inter-reuse time** (given it recurs) = **log-normal survival** `S(Δt)=1−F_LN(Δt;μ,σ)` (human
   interactivity times are log-normal; why SAECache uses it). Hazard is non-monotone (matches "if not
   reused in 10 min, wait till next session").

## Closest prior + why short-horizon
- **SAECache** (2605.18825) — per-class log-normal survival + token-type weights (System **92%** vs CoT
  **2%**, 42–756× span) + online MLE+EMA; the template to extend. Short-horizon: `(μ,σ)` fit only on
  *observed* intervals within cache residency → hour/day recurrences are never observed (censored);
  cross-session treated as negligible (<0.01%).
- **LPC** (NeurIPS'25) — 118M continuation predictor, but **one coarse score per conversation** (system
  prompt and throwaway CoT get the same priority); within-session signal, no cross-session channel.
- **KVFlow** (2507.07400) — workflow steps-to-execution; *deterministic* schedule signal (next few
  steps), not a stochastic hour/day prior — complementary, B8 covers the open-world case.
- Orthogonal (not competitors): IMPRESS / "Predicting Future Utility" = *within-prefix token* importance.

## Borrow from web-cache theory
- **LRB (NSDI'20)** — the key trick: don't regress exact next-access time (heavy-tailed, censored);
  **relax to the Belady boundary** = binary "next access beyond horizon?" (horizon ≈ 2× cache size).
  Features = 32 inter-access **deltas** + 10 **EDCs** (multi-timescale decayed counts) + static; GBM on
  log(time-to-next); 64-sample/30µs; **LRU fallback during warmup**. ⟹ **binary "long-lived vs not" is
  more robust than exact-time regression** (also Hawkeye/Glider).
- **LHD (NSDI'18)** — hit-density `P[hit|age]/E[remaining lifetime|age]` from age-binned histograms per
  class; 64-object sampling; **1% explorers** (never-evict a random fraction → keep gathering the
  long-horizon labels that are otherwise invisible).
- **GL-Cache (FAST'23)** — learn at **group level**, not per-block (228× throughput over object-level).

## Features + classes (cheap; mostly already on a block/node)
Token-type (biggest axis), recency `Δt`, reuse-count, EDCs (~10 floats), age, prefix-depth, session-active
(ref-count), tenant/LoRA (cross-tenant reuse ≈ 0 → key popularity by tenant). **Class = (token-type ×
tenant/LoRA × coarse-popularity-bucket × diurnal-bucket)** — group learning accumulates samples for the
thin tail + amortizes overhead.

## Calibration + safety (what makes a miscalibrated estimator safe — non-negotiable)
- **Online**: MLE + variance-adaptive EMA (β low when variance high → track drift); fit per class when
  n≥~20. Ever-reuse Bernoulli via eviction-feedback EWMA. Diurnal class key for the measured day/night drift.
- **Shrinkage to recency (the key robustness knob)**: empirical-Bayes blend of class MLE with parent-group
  pooled estimate, weight ∝ n_class → **n→0 degrades gracefully to LRU** ("behave like LRU until evidence").
- **Competitive-safety blend** (learning-augmented caching, Lykouris–Vassilvitskii ICML'18): the
  Predictive-Marker bound `CR ≤ min(2+4√(η/OPT), 4·H_k)` — consistency + robustness simultaneously. Instantiate:
  the learned score is a **tie-breaker/re-ranker inside an S3FIFO frame**, `priority = robust_rank +
  λ·confidence·learned_score`, **λ→0 for under-sampled / recently-mispredicting classes**. S3FIFO (not LRU)
  is the robust base — its probationary FIFO already quick-demotes the >50%-never-reused mass.

## Hot-path form (runs at eviction rates, no model inference)
64-candidate sampling (LHD/LRB) → per candidate a **closed-form** score (survival form:
`w_type·p_ever·(1−F_LN(Δt;μ_c,σ_c))/Δt` = arithmetic + one `erfc`; or LHD hit-density from histograms) →
evict lowest; reserve **1% explorers**. **Background daemon** (GL-Cache cadence) updates per-class
`(μ,σ,p_ever,EDC,w_type)` from an access/eviction ring buffer, publishes lock-free via double-buffer.

## Honest bottom line
- Short-horizon mass (80–90%): can match SAECache/LRB (well-calibrated, high good-decision-ratio).
- **Hour/day tail: intrinsically sample-starved + partly irreducible (censoring)** — you get a **coarse**
  signal (per-class ever-reuse + token-type prior: system/RAG = keep-long, CoT = evict-now), **not** sharp
  hour-resolution survival. The win = **avoid catastrophic eviction of the high-value tail**, not precise timing.
- Failure modes designed against: recompute storm (→ safety blend), cold-start/drift (→ shrink-to-LRU +
  diurnal/tenant keys), survivorship bias (→ explorers + sample-all-in-window), heavy-tail underfit (→
  factorized model + binary target), cross-user leakage (→ tenant-keyed popularity).

## Phased design
- **P1 (MVP, ship first):** a custom `CachePolicy` returning `priority = w_type[type]·(1−F_LN(Δt;μ_c,σ_c))/Δt`;
  **classes = token-type only** (system/user/tool/response/CoT); `(μ,σ)` online MLE+EMA (fit n≥20, shrink to
  global when fewer); `w_type` **fixed prior from SAECache's measured table** (system≈0.92 … CoT≈0.02),
  EWMA-refined from eviction feedback; wrapped as a **tie-breaker inside S3FIFO** + **LRU warmup** +
  confidence-gated λ. **Zero new model inference**; reuses recency/hit-count/age; adds a token-type tag at
  insert + ~5 per-class floats. Provably no-worse-than-LRU by the learning-augmented bound. Captures the
  dominant 42–756× token-type axis day one.
- **P2 (full):** EDCs + tenant/LoRA + popularity-bucket + diurnal class keys (GL-Cache group learning) +
  the factorized ever-reuse Bernoulli + LHD hit-density + a background GBM on the relaxed-Belady binary
  target as a research upper-bound comparison.

## Sources
SAECache 2605.18825 · LPC NeurIPS'25 · KVFlow 2507.07400 · "KVCache in the Wild" 2506.02634 · Mooncake
2407.00079 · LRB NSDI'20 · LHD NSDI'18 · S3FIFO SOSP'23 · GL-Cache FAST'23 · Hawkeye/Glider ISCA'16/MICRO'17 ·
learning-augmented caching (Lykouris–Vassilvitskii ICML'18; Wei 2005.13716; Rohatgi 1910.12172).
