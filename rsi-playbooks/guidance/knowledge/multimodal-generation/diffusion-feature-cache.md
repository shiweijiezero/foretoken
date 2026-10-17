<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce diffusion compute through feature and solver-target reuse

English | [简体中文](diffusion-feature-cache_zh.md)

## Observations and Motivation

Diffusion transformers repeatedly evaluate nearby latents. Much of the expensive hidden computation changes slowly between denoising steps, while the solver still needs an updated prediction at each step. TeaCache reuses residuals, TaylorSeer forecasts features, and AutoTarget chooses the quantity exposed to the solver. These training-free methods reduce transformer evaluations by approximating the trajectory; they do not accelerate an unchanged computation losslessly.

## Principles and Analysis

TeaCache measures relative L1 change in timestep-modulated input, maps it through a model-fitted polynomial, and accumulates the predicted change since the last refresh. A small accumulated change permits residual reuse. TaylorSeer instead estimates feature derivatives from finite differences and predicts `F(t+δ) ≈ F(t)+δF′(t)+δ²F″(t)/2`. Forecasting captures gradual drift that holding a residual constant misses, but abrupt changes undermine either approximation.

The solver-facing target also matters. For linear flow `x_t=(1−t)c+te` and `v=e−c`, an anchor yields `c=x_a−t_a v_a` and `e=x_a+(1−t_a)v_a`. Under AutoTarget's stated deterministic-Euler conversion assumptions, velocity, clean-endpoint, and noise-endpoint reuse produce the same between-anchor trajectory. Reusing displacement `h_a v_a` does not: with a new interval `h_j`, the error is proportional to `(h_j−h_a)v_a`.

## Design

Keep cache state inside one generation: anchor features, finite differences or residuals, and the refresh indicator. TeaCache's threshold controls refresh frequency; TaylorSeer's order and interval control forecasting cost. AutoTarget calibrates admissible solver targets on matched exact states using decoded RGB error and abstains when candidate scores are too close. Its target selection is distinct from TaylorSeer and can address few-step pipelines where feature forecasting is unsuitable.

Use the original checkpoint, text encoder, VAE, and scheduler together; none of these caches supplies replacement weights. In the cited vLLM-Omni integration, cache-dit applies to request-level diffusion execution, not step execution. The guide advises against TaylorSeer on few-step distilled models.

## Implementation

A TeaCache-style residual loop is:

```text
residual, previous, accumulated = empty, empty, 0
for t in solver.schedule:
    h = patch_embed(x); u = timestep_modulate(h, t, conditioning)
    if previous exists: accumulated += calibrated_relative_change(u, previous)
    if residual is empty or refresh_required(t, accumulated):
        residual = transformer_blocks(h, t, conditioning) - h
        accumulated = 0
    prediction = project_and_unpatchify(h + residual, t)
    x, previous = solver.step(prediction, t, x), u
return VAE.decode(x)
```

Boundary refreshes and calibration follow the model implementation. TaylorSeer replaces residual hold with a finite-difference forecast. Cache tensors retain the pipeline's precision; their storage, reductions, and prediction operations add work even when a transformer call is skipped.

## Impact and Results

TeaCache v1 reports OpenSora-Plan, 65 frames at 512² with 150 sampling steps, on one A800 80GB: latency falls from 99.65 s to 22.62 s, a 4.41× speedup at threshold 0.1, while VBench changes from 80.39% to 80.32%. At threshold 0.2, latency is 14.60 s (6.83×), with VBench 79.72%: more aggressive reuse trades quality for speed.

TaylorSeer v1 reports FLUX.1-dev on 200 DrawBench prompts at 1000×1000, a 50-step baseline, and H800: interval 6/order 1 gives 3.53× latency speedup; Image Reward is 0.9953 versus 0.9898 without caching. Its 4.99× FLOPs reduction is not the latency speedup. VAE, text encoding, and delivery remain, so saved transformer work does not translate proportionally into service latency. Forecasting adds cache memory and can change prompt fidelity, details, motion, and diversity.

## References

- [TeaCache, arXiv:2411.19108v1](https://arxiv.org/html/2411.19108v1) and [author code](https://github.com/ali-vilab/TeaCache): residual and cumulative-proxy mechanism as background.
- [TaylorSeer, arXiv:2503.06923v1](https://arxiv.org/html/2503.06923v1) and [author code](https://github.com/Shenyi-Z/TaylorSeer): forecasting recurrence and model entry points.
- [AutoTarget, arXiv:2610.03577v1](https://arxiv.org/html/2610.03577v1): submitted 2026-10-02; method. [Public implementation](https://github.com/wali1024-offical/AutoTarget/tree/e91a428b8bbc37a6661576a182e39e94e3cc55dd) at `e91a428`.
- [Omni execution modes](https://docs.vllm.ai/projects/vllm-omni/en/latest/user_guide/diffusion/execution_modes/) and [cache-dit documentation](https://github.com/vllm-project/vllm-omni/blob/37f094a388ba1c79f83c129fda39797c1ebfc2a4/docs/user_guide/diffusion/cache_acceleration/cache_dit.md): source at `37f094a`; release `v0.30.0` was published 2026-09-25.
