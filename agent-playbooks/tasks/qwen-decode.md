# Qwen Decode Optimization

English | [简体中文](qwen-decode_zh.md)

## Goal

Improve single-request decode speed for Qwen3.5-35B-A3B BF16 while preserving answer quality. Compare time per output token (TPOT) and output tokens per second.

## Environment

Two machines, each with eight MetaX C500 GPUs. Use Qwen3.5-35B-A3B BF16; the agent selects the GPU count and parallelism based on measurements.

## Optimization scope

There is no restriction on which components may be optimized.
