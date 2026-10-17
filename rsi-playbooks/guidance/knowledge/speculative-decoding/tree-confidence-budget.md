<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Allocate verification nodes by draft path confidence

English | [简体中文](tree-confidence-budget_zh.md)

## Observations and Motivation

A draft tree supplies alternatives that can rescue an early wrong guess, but target verification computes every retained node while the response commits one path. More nodes therefore do not directly mean more output. A fixed verification budget must balance depth, alternatives, and execution cost.

## Principles and Analysis

EAGLE-2 scores each node by the product of draft probabilities along its root-to-node path. A descendant inherits every ancestor's uncertainty, so a deep confident-looking token below a weak root is not independently promising. Global reranking with shallow tie-breaking retains ancestor closure. Branches are mutually exclusive; summing node survival scores is a construction heuristic, not the expected output of every stochastic tree verifier.

## Design

Use one target and one compatible trained EAGLE module, not one model per branch. Expand promising frontier nodes, retain a node budget, and flatten the tree for one target pass. Positions and masks expose only the committed prefix and each node's ancestors; siblings cannot attend to one another. Greedy verification follows target-matching children until correction. Stochastic verification uses the engine's tree coupling with proposal probabilities and sibling bookkeeping, not independent p/q tests followed by longest-branch selection.

## Implementation

The conceptual algorithm is:

```text
frontier = committed root
expand frontier with draft module; keep token, parent, conditional probability
path_score[node] = path_score[parent] * probability[node]
rerank globally; retain budget with ancestor closure and shallow tie-breaking
build ancestor-only attention mask and positions; run target
verify one valid path; commit it plus correction/bonus; discard other nodes
```

SGLang's pinned EAGLE worker owns execution. `--speculative-num-steps`, `--speculative-eagle-topk`, and `--speculative-num-draft-tokens` control depth, branching, and capacity: leave all unset for automatic choice or set all explicitly. With topk=1, capacity becomes steps+1. The documented overlap scheduler supports topk=1; wider trees use a different scheduling path. Matching EAGLE3 weights can supply the newer draft source without changing this outer tree process.

## Impact and Results

Path-confidence allocation avoids spending deep nodes on prefixes unlikely to survive and preserves early alternatives. It adds draft expansion rounds, tree masks, candidate LM-head rows, and path compaction; non-selected nodes never become output. Predictable continuations favor depth, while early ambiguity favors breadth. Wider trees can also lose host/device overlap, so equal node counts do not imply equal iteration time. After commit, only the chosen history survives; [recurrent replay](../attention-state/state-write-replay.md) addresses model state not recoverable from KV-node retention alone.

## References

- [EAGLE-2 paper, v2](https://arxiv.org/html/2406.16858v2): dynamic expansion, global path scores, reranking, and masks (2024).
- [Pinned SGLang guide](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/docs/docs/advanced_features/speculative_decoding.mdx): current tree parameters and overlap constraints; [EAGLE worker](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/speculative/eagle_worker_v2.py) owns the worker execution path.
- [EAGLE-3 paper](https://arxiv.org/html/2503.01840) establishes the separately trained feature-conditioned draft source. See [training alignment](draft-rollout-training.md) for that distinct responsibility.
