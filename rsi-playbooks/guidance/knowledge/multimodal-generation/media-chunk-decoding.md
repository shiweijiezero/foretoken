<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce first-media latency through chunked decoding and delivery

English | [简体中文](media-chunk-decoding_zh.md)

## Observations and Motivation

Text-token streaming does not automatically provide playable speech or video. A waveform decoder may require future codec blocks; a video VAE may wait for a latent chunk; bytes can arrive before a player can use them. Chunked generation targets first usable media and continuous playback, rather than merely the first generated code.

## Principles and Analysis

The pipeline overlaps stages: Thinker supplies text and hidden-state conditioning, Talker emits codec chunks, and Code2Wav decodes one chunk while Talker generates the next. Decoder lookahead sets a lower bound on startup. Qwen2.5-Omni's waveform path uses a flow-matching DiT and modified BigVGAN; its DiT attends to two history blocks, the current block, and one future block. That detail belongs to Qwen2.5, not automatically Qwen3.

If a chunk contains `D` seconds of media and arrives every `A` seconds, sustained playback requires average `A≤D`. `A<D` allows buffer recovery after gaps, but a sufficiently long tail gap still causes an underrun.

## Design

Keep request correlation, chunk order, and allowed history across the asynchronous stages. The orchestrator coordinates cancellation and releases buffers after their consumers finish. A bounded producer/consumer queue can control slow-client accumulation; the cited architecture does not prescribe one universal credit protocol.

The vLLM-Omni Qwen3-Omni Thinker→Talker→Code2Wav pipeline is separate from diffusion-video output. The latter uses `WS /v1/realtime/video`, sending `video.chunk_metadata` JSON immediately before its binary fragmented-MP4 chunk. The documented format is `m4s`; this is generated video output, not live conversational video input.

## Implementation

The chunk delivery sequence is:

```text
start request stages with shared request identity
while generator has output and request is active:
    obtain next codec or latent chunk with its sequence index
    wait until the model-required lookahead is available
    decode chunk using compatible history and decoder weights
    package playable audio or fragmented-MP4 payload
    for video: send metadata, then its associated binary frame
    retain history needed by the next chunk; release consumed buffers
on cancellation: stop stages and retire outstanding owned transfers
```

Use compatible released text, codec, waveform, and VAE assets. Chunk delivery needs no new training; [few-step causal distillation](few-step-distillation.md) is a separate model change. The model must support incremental decoding and the diffusion pipeline must implement step execution and chunk emission. A streaming flag cannot turn a final-only model into one. Vocoder/VAE precision and CUDA dependencies can differ from the BF16/FP16 transformer path.

## Impact and Results

Chunking removes the wait for complete-answer generation before decoding and delivery can start. Overlap reduces the serial portion of the path; lookahead, initial conditioning, VAE/vocoder execution, and packaging still delay the first playable chunk. Earlier codec output alone does not remove these costs.

Smaller chunks lower buffering latency but increase per-chunk boundary, launch, and packaging overhead. Reducing model lookahead or truncating history additionally changes speech continuity or video motion and identity, whereas transport-only chunking preserves model semantics. Slow clients can accumulate queued bytes unless production follows consumption. A favorable average real-time factor can still coexist with underruns when delivery is bursty.

## References

- [Qwen2.5-Omni report, arXiv:2503.20215v1](https://arxiv.org/html/2503.20215v1): speech architecture and block lookahead; video is input, not generated output in this report.
- [Omni architecture](https://github.com/vllm-project/vllm-omni/blob/37f094a388ba1c79f83c129fda39797c1ebfc2a4/docs/design/architecture_overview.md) and [video API](https://github.com/vllm-project/vllm-omni/blob/37f094a388ba1c79f83c129fda39797c1ebfc2a4/docs/serving/streaming_video_output_api.md): contents at `37f094a`. These are implemented upstream paths with model-specific restrictions; release `v0.30.0` predates this main revision.
