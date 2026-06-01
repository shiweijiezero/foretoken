# 05 — MTP speculative decoding (module C1 = use; C2 = build)

Grounded in the vLLM fork (v0.22.0) + DeepSeek-V3 / SGLang. **C1 (MTP itself) = USE vLLM's
native MTP — do NOT build a drafter. C2 (goodput-aware adaptive MTP control) = BUILD — the
defensible slot.**

## What MTP is
The target model's **own natively-trained multi-token-prediction layer** repurposed as the drafter
(DeepSeek-V3 §2.2; GLM-4.5 `glm4_moe_mtp.py`). Structurally EAGLE-style: `eh_proj(cat[norm(embed
next_tok), norm(prev_hidden)])` → one extra transformer block (a MoE block, reuses the shared
embed/head). DeepSeek/GLM ship **`n_predict = num_nextn_predict_layers = 1`** MTP layer **inside the
checkpoint** → for a 200B+ MoE with no off-the-shelf drafter and no post-hoc EAGLE head, **MTP is the
only zero-extra-training high-accept drafter**. MoE decode is memory-bound → accepting several tokens
per target forward amortizes hugely.

## vLLM support (precise, file:line) — C1 is a clean USE
- MTP rides the **EAGLE proposer path**: `use_eagle()` returns True for mtp (`speculative.py:1059`);
  MTP-model-types normalize `method="mtp"` (`speculative.py:704`); runner routes to `EagleProposer`
  (`gpu_model_runner.py:594`) = thin `SpecDecodeBaseProposer` subclass.
- Config: `{"method":"mtp","num_speculative_tokens":1}` — **no separate draft model** (weights in the
  target checkpoint). `num_speculative_tokens` defaults to `n_predict`(=1); must be **divisible by
  n_predict**; **>1 reuses the single MTP layer cyclically → accept degrades** (`speculative.py:708,766`).
- **Chain (not tree)** propose loop (`llm_base_proposer.py:570`); standard rejection-sampler verify
  (`rejection_sampler.py`) → **lossless**.
- **Async scheduling: MTP is gated IN** (`EagleModelTypes`, `vllm.py:911`) — a real serving edge MTP has
  over the (custom_class) cross_vocab path, which is gated OUT. Requires `disable_padded_drafter_batch=false`.
- **CUDA graph: draft step is PIECEWISE-only** (`gpu_model_runner.py:5856`); target MoE can be
  FULL_AND_PIECEWISE. Draft batch sizes are captured via `adjust_cudagraph_sizes_for_spec_decode`.

## Accept / speedup in practice
- DeepSeek-V3 (paper §5.4.3): **2nd-token accept 85–90% → 1.8× TPS.**
- SGLang DeepSeek-V3 16×H200 (EP+PD): 3-tok **accept 2.18**, 4-tok **2.44, +60.8% tput**; **128×H200
  2-tok only +14.2%** (gain shrinks at scale/high concurrency); GLM-4.5-Air **1.3–1.8×** (single MTP
  layer not fully optimized for deep draft).
- **Regime:** big win at **low/moderate batch + long context**; can go **negative at high batch +
  short context on MoE** (Cascade arXiv 2506.20675: 3 draft tokens → up to 3× expert activation;
  static K can be **1.5× slowdown**; even K=1 loses >25% on some math). MagicDec: long ctx flips positive.

## Interaction with our KV + scheduling (cross-module constraints)
- **Draft KV is a SEPARATE `kv_cache_gid`** (`llm_base_proposer.py:1520`); scheduler reserves
  `num_lookahead_tokens` extra blocks only in decode (`scheduler.py:705`). **⟹ HARD CONSTRAINT for our
  B2/B3 modules: tag the drafter's `kv_cache_gid` as EPHEMERAL — never promote draft KV to the offload
  tier or cache it as a prefix** (rejected drafts write KV that's discarded). Not automatic today; needs
  an explicit tag.
- **C2 hook:** spec length is a single static int (`num_lookahead_tokens`, `scheduler.py:213`) — the
  injection point for a load-adaptive controller. **Constraint:** dynamic K must stay within captured
  **cudagraph buckets** or eager-fallback erases the gain (sharpest risk).

## C2 = the build slot (goodput-aware adaptive MTP control)
**No adaptive spec length exists in vLLM** (grep confirms: no goodput / disable_by_batch_size /
per-request K; only static `num_speculative_tokens` + a static `synthetic_acceptance_rates`). The
literature defining the target — SmartSpec (2406.14066, dense), AdaServe (2501.12162, SLO-customized),
**Cascade (2506.20675, the only MoE-aware, not in vLLM, not MTP-specialized)** — none is in vLLM.
**C2's defensible niche:** a goodput/utility-aware controller that picks `num_speculative_tokens`
(and whether to spec at all) **per step** from observed accept-rate + batch/KV load + MoE
expert-staging cost, **bucket-constrained**, specialized for MTP-on-EP'd-MoE. Genuinely missing upstream.

## Build-vs-use + integration plan
- **C1 = USE** vLLM native MTP. Tuning only: pick `num_speculative_tokens` (1, sweep 1–4 knowing accept
  degrades); keep `disable_padded_drafter_batch=false` (async); EP on (MTP verify is EP-transparent);
  **tag draft `kv_cache_gid` ephemeral**.
- **C2 = BUILD** the controller on the `num_lookahead_tokens` hook + accept feedback, bucket-constrained.
- **Target = GLM-4.5-Air-106B** (`Glm4MoeMTP`, A100-runnable; DeepSeek-V3 is NO-GO on A100 — FP8 KV).
  Qwen3-235B-A22B has MTP variants if a bigger MoE is wanted.
- **Phase A (C1 baseline):** GLM-4.5-Air + `method=mtp` K∈{1..4}, TP+EP; measure accept/TPS/per-stage
  vs no-spec across batch{1,4,8,16}×ctx{1k,8k,32k} → the regime map. **Phase B (C2):** the controller;
  compare vs static-K + SmartSpec/Cascade baselines. Lossless at temp=0; GLM official sampling for realism.

## Open questions
1. Cudagraph re-capture cost of dynamic K (stay in buckets vs eager fallback) — sharpest risk.
2. MTP depth ceiling on GLM (`n_predict=1`): how fast does accept fall at K=2–4? → C2's useful K range.
3. EP × draft expert-activation contention at high batch (Cascade says it changes optimal K).
4. Async scheduling × dynamic K (padded-batch invariant).
5. Draft-KV exclusion from offload/prefix promotion (the ephemeral tag) — explicit, not automatic.

## Sources
DeepSeek-V3 2412.19437 (§5.4.3) · SGLang MTP (lmsys 2025-07-17) · Cascade 2506.20675 · SmartSpec
2406.14066 · AdaServe 2501.12162 · MagicDec 2408.11049 · vLLM MTP doc + fork file:line (above).
