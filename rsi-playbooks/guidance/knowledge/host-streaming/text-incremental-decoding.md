<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce stream-rendering work through incremental text decoding

English | [简体中文](text-incremental-decoding_zh.md) · [Knowledge](../README.md)

## Observations and Motivation

Token IDs are not independent printable chunks. Byte-level tokenizers can split Unicode characters, decoder rules can depend on preceding tokens, and stop strings or tool parsers can withhold already computed text. Re-decoding the whole prefix at each step also makes long outputs increasingly expensive on the host, even when device cadence stays regular.

## Principles and Analysis

If decoding cost is linear in input length, decoding prefixes 1 through n requires `Σj=O(n²)` work. Incremental decoding consumes new tokens plus boundary context instead. Rebuilding accumulated strings or replaying a parser can still reintroduce growing costs.

Excluding stop strings of maximum length L requires withholding up to L−1 characters while generation continues, so a future stop marker does not include already emitted text. Batching k tokens amortizes fixed output cost a to a/k, but under regular interval d adds mean waiting `(k−1)d/2`. This is an explanatory timing model, not a measured speedup.

## Design

Maintain per-request decoder context, stable-text offset, pending stop suffix, and parser state. Emit only newly stable text and retain the unresolved suffix. Completion flushes all permissible text, including a partial output group; cancellation drops pending output and releases state.

Use the tokenizer's supported incremental decoder rather than decoding each token separately. Compatible requests can share batched decode calls, but their offsets, stop rules, and terminal states remain independent.

## Implementation

The core state update is:

```text
on new token IDs:
    extend incremental decoder; obtain newly stable text
    append stable text to pending suffix
    search stop strings, including matches crossing the old/new boundary
    if matched:
        emit permitted prefix, with stop text only if configured
        finish request
    else:
        emit pending text except the final L−1 characters
        retain that suffix (retain zero characters when L=0)
on normal completion:
    finalize decoder; apply stop rule; flush remaining permitted text
on cancellation:
    discard pending text and release request decoder/parser state
```

vLLM supplies native `DecodeStream` and helper-based paths with delta output; SGLang tracks surrogate/read offsets and deferred chunks. Fastokens supports selected BPE paths, documented from vLLM 0.23.0 with fastokens ≥0.2.0. Reasoning/tool parsers may need additional context beyond plain text decoding. Foretoken's received-chunk timing counts non-empty `choices`, including role/finish chunks, not individual tokens.

## Impact and Results

Incremental decoding removes repeated full-prefix conversion; suffix-only responses avoid repeated copying of accumulated text. Stop buffering preserves output correctness at the cost of content-dependent visible delay. Grouped emission reduces fixed per-event processing but adds waiting. More decoder workers do not solve a saturated shared output queue, and larger chunks can raise observed ITL while token throughput improves.

## References

- [vLLM incremental detokenizer](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/engine/detokenizer.py): native/helper paths, stop buffer, and delta output.
- [SGLang detokenizer manager](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/managers/detokenizer_manager.py): incremental offsets, deferred text, batched decoding, and final cleanup.
- [vLLM optimization documentation](https://docs.vllm.ai/en/latest/configuration/optimization/): fastokens version and tokenizer restrictions.
- [Foretoken chunk timing](../../../../benchmarks/src/benchmarks/integrations/streaming.py) and [metric definitions](../../../../benchmarks/docs/metrics.md): Foretoken `e6ae3df6`; ITL is a received-chunk interval.
