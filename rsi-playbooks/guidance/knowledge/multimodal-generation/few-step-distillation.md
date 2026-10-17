<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Distill few-step generation through consistency and distribution matching

English | [简体中文](few-step-distillation_zh.md)

## Observations and Motivation

Few-step diffusion reduces repeated network evaluations by training a different predictor, rather than making the original sampler's kernels faster. Consistency distillation teaches large jumps along a denoising trajectory. For causal video, it must also address exposure bias: inference conditions later chunks on imperfect student-generated history, whereas teacher-forced training supplies clean history.

## Principles and Analysis

A consistency objective makes predictions from different noisy states on one teacher trajectory agree on a common denoised output. Guidance can be distilled into the student, changing its sampling recipe. LCM-LoRA represents this update as low-rank weights for a specified base checkpoint, not a universal adapter.

Causal-rCM supplements teacher-forcing consistency training with self-forcing distribution-matching distillation (DMD). The student rolls out its own history. A frozen teacher estimates the real-distribution score; a trainable fake-score network estimates the student's distribution. Their score difference provides a direction for updating the student, while the fake-score network learns from student samples. This targets distributional agreement, not sample-by-sample equality.

## Design

The causal guide first adapts a bidirectional Wan model into a causal teacher, performs teacher-forcing consistency distillation, then refines with self-forcing DMD. The final training stage has three diffusion-network roles: student, frozen bidirectional real-score teacher, and trainable fake-score network. Only the student is needed for inference, alongside text encoder and VAE.

Ordinary chunk inference performs an additional clean zero-time context pass after each chunk. Removing that pass by retaining the last noisy context requires matching noisy-context training and chunk settings. The generic rCM dCM→sCM→DMD-with-sCM recipe and this causal sequence are distinct configurations.

## Implementation

The self-forcing stage and serving path are:

```text
initialize student from consistency-distilled causal checkpoint
freeze real_score_teacher; initialize trainable fake_score
history, clip = student.rollout(prompt, few_step_schedule)
x_t = add_noise(clip, sampled_t)
real, fake = real_score_teacher(x_t, prompt), fake_score(x_t, prompt)
update student using the DMD score-difference direction
fit fake_score on detached student samples with its denoising loss
at inference: generate each chunk with student and retained history
refresh clean context unless trained noisy-context reuse is enabled
stream VAE-decoded chunks; retain neither score network
```

rCM publishes CUDA/PyTorch training code, FSDP2/FlashAttention configurations, and asset links for Wan2.1 T2V 1.3B/14B, VAE, and umT5. The cited guides name distilled checkpoints but do not supply a complete downloadable student inventory. Obtain a compatible trained student or reproduce training; downloading the Wan teacher alone does not enable few-step generation. LCM supplies released LCM/LCM-LoRA families.

## Impact and Results

The direct saving is fewer student transformer evaluations. Total latency remains `N_student*T_DiT + T_context + T_text + T_VAE + T_delivery`; context passes and decoding can dominate once denoising becomes short. The Causal-rCM guide supports four denoising steps per chunk, plus one clean-context pass: five transformer forwards, not four. Its two/one-step schedules keep four steps for the first chunk and reduce later chunks. Noisy-context reuse removes the extra pass only with matching training. Self-forcing trains on generated histories and reduces the clean-history mismatch, at the cost of student rollouts and two score networks during training.

Distribution matching can improve realism without preserving all prompt details or diversity modes. Long causal rollouts can drift, repeat, or change subjects. BF16/FP16 configuration and guidance belong to the trained operating point; the LCM_Dreamshaper_v7 example uses FP32 and notes a possible quality cost from FP16. Fewer denoising steps do not imply a proportional speedup for complete media delivery.

## References

- [NVIDIA rCM](https://github.com/NVlabs/rcm/tree/ed3cb14dd936f92cdc9f9381af7369991509b41f): README, asset instructions, and causal training entry points; commit `ed3cb14`.
- [Causal-rCM guide](https://github.com/NVlabs/rcm/blob/ed3cb14dd936f92cdc9f9381af7369991509b41f/Causal_rCM.md): causal training/inference entry point; availability is model/configuration-specific.
- [LCM author repository](https://github.com/luosiallen/latent-consistency-model): training and released LCM/LCM-LoRA families.
