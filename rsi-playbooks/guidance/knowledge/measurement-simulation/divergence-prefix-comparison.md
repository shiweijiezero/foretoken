<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Diagnose generation divergence through fixed-prefix and task comparisons

English | [简体中文](divergence-prefix-comparison_zh.md) · [Knowledge](../README.md)

## Observations and Motivation

A kernel change can alter logits without changing tokens; a tiny perturbation can also swap near-tied tokens and send autoregressive generation into a different context. Neither observation alone establishes answer-quality change. Diagnosis must separate local numerical fidelity, sequence agreement, and task accuracy rather than treating visible-text mismatch as one failure.

## Principles and Analysis

Floating-point addition is non-associative, so batching, reduction order, and precision can change results even with a fixed seed. Let top-logit margin m=z₁−z₂>0. A componentwise error bound ε preserves argmax when m>2ε; RMSE is not such a bound. As an explanatory example, logits 10.000 and 9.999 flip under −0.0006 and +0.0006 perturbations.

For identical prefixes, `KL(p||q)=Σpᵢ log(pᵢ/qᵢ)` measures reference-weighted distribution shift, and `TV=½Σ|pᵢ−qᵢ|`. After different generated tokens enter the prefix, later mismatch includes context drift. Common logit offsets cancel under softmax, motivating centered-logit RMSE.

## Design

Teacher-force the same token-ID prefixes into both models, with identical vocabulary mapping, template, and special-token rules. Retain full-vocabulary distributions and per-position metrics; top-k output cannot establish full-vocabulary KL, and decoded-text keys can collide.

Separately compare native greedy token sequences and first-divergence positions, then pair task scores under the same scoring rule. Repeat the unchanged reference to characterize its own variability. vLLM batch invariance offers a diagnostic control within its documented beta hardware/model/TP scope, with different kernel choices and performance cost.

## Implementation

At each fixed-prefix position:

```text
lp = reference_logits − logsumexp(reference_logits)
lq = candidate_logits − logsumexp(candidate_logits)
p = exp(lp); q = exp(lq)
KL = sum(p[i] * (lp[i]−lq[i]) for i where p[i] > 0)
TV = 0.5 * sum(abs(p−q))
centered_RMSE = sqrt(mean(((lp−mean(lp))−(lq−mean(lq)))**2))
top1 = argmax(lp) == argmax(lq)
topk_overlap = size(topk(lp) intersect topk(lq)) / k
retain reference top-two margin and corpus-token probability change
```

Use stable log-softmax, retain the full vocabulary, and treat positive reference mass with zero candidate probability as infinite KL rather than clipping it away. When responses serialize probabilities, check coverage and mass before normalizing rounding error. Foretoken's comparison client provides teacher forcing, token-ID keys, complete-vector metrics, and native greedy IDs. For a greedy mismatch, score the shared pre-divergence prefix; paired task evaluation then determines whether changed continuations change correctness. Issue #51187 reports logprob variability with unchanged emitted IDs, illustrating the distinction.

## Impact and Results

Fixed-prefix metrics isolate numerical changes from autoregressive context drift. Margin inspection separates near-tie flips from large local distribution shifts; native IDs expose EOS and token agreement without retokenization artifacts. Paired task scores distinguish alternative correct answers from actual quality loss. Each comparison has its own outcome: distribution closeness does not imply bitwise reproducibility, and sequence mismatch does not imply degraded task accuracy.

## References

- [PyTorch 2.9.0 numerical accuracy](https://github.com/pytorch/pytorch/blob/v2.9.0/docs/source/notes/numerical_accuracy.rst): batching, reduction order, backend precision, and non-bitwise guarantees; version-pinned text.
- [vLLM batch invariance](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/batch_invariance.md): beta scope and trade-offs. [Issue #51187](https://github.com/vllm-project/vllm/issues/51187): report distinguishes logprob variation from token divergence.
- [Foretoken distribution comparison](../../../../benchmarks/docs/eval/distribution-comparison.md) and [comparison client](../../../../benchmarks/src/benchmarks/integrations/distributions.py): at `e6ae3df6`; teacher forcing, native greedy IDs, complete probability vectors, and metric formulas.
