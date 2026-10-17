<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce Startup Cost Through Sharded Checkpoints and Pipelining

English | [简体中文](startup-sharded-checkpoints_zh.md)

## Observations and Motivation

A downloaded model is not a ready model. Startup still resolves checkpoint files, allocates tensors, reads and installs weights, transforms them for the runtime, and prepares execution. Optimizing file reads cannot remove every later stage, and a cached checkpoint is not necessarily a cached runtime representation.

## Principles and Analysis

For a checkpoint of size W, a sequential estimate is `T_read = W/B_storage`; startup additionally pays installation, H2D copies, transformation and runtime preparation. In a genuinely overlapped pipeline, steady-state chunk time approaches the slowest stage rather than their sum. Startup, final drain and model-dependent work remain. Only actual chunk overlap gives this bound.

Consider TP4 loading a globally stored 40 GiB checkpoint. Reading and slicing all weights on every rank may produce much more aggregate I/O than reading one roughly 10 GiB rank-local representation. This is only an illustrative balanced case: replicated tensors, expert assignment, padding and runtime quantization change the numbers. Skipping irrelevant tensor reads and converting once into a rank-local checkpoint address different costs.

## Design

Prepare a topology-specific checkpoint outside the repeated startup path. Save the runtime parameters each TP/PP rank owns, excluding duplicated aliases/views. At startup, construct the matching model and copy those tensors directly into prepared parameters. File names identify rank coordinates, not the TP/PP sizes; deployment must retain the topology that gives them meaning.

A chunked loader can additionally pipeline storage, host staging and device copies. Sharding avoids irrelevant reads and repeated slicing; pipelining hides exposed stage time. They are complementary, but neither replaces compilation, graph capture or other execution preparation.

## Implementation

The cited `ShardedStateLoader` follows a direct installation path:

1. Build the model for the intended TP/PP assignment and enumerate its parameter storage.
2. Filter aliases and overlapping views when producing rank-local safetensors, so storage is not saved twice.
3. At load time, resolve only the files matching the current TP rank and, when enabled, PP rank.
4. Read each saved tensor and copy it into the corresponding prepared parameter or valid subview; report missing required tensors.
5. Complete the model's applicable post-load processing and runtime preparation before serving.

This loader does not perform arbitrary runtime quantization or resharding. Default loading remains responsible for model-specific fused names, slices and transformations; its local-expert filtering also depends on expert-load-balancing requirements. ServerlessLLM uses aligned GPU-specific binary chunks plus an offset index for its pipeline. ModelExpress is another existing vLLM loading path; GMS allocation artifacts require a different consumer from named tensor checkpoints.

## Impact and Results

Rank-local checkpoints remove repeated full-checkpoint reads and model slicing where parameters are truly partitioned. Their cost is a prepared representation tied to the model, dtype and topology; changing that topology requires supported conversion rather than renaming files. Replicated tensors and expert assignments determine actual byte savings.

Pipelining overlaps stage work but startup remains limited by the slowest loading stage plus finalization. Weight-loading improvements therefore reduce only their portion of process-to-first-generation time. Mooncake publication metadata and LMCache KV persistence do not eliminate weight installation.

## References

- [DefaultModelLoader](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/default_loader.py)
- [ServerlessLLM, January 2024](https://arxiv.org/html/2401.14351v1)
- [ShardedStateLoader](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/sharded_state_loader.py)
- [ModelExpress](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/modelexpress_loader.py)
- [weight artifact code](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/gpu_memory_service/v1/snapshot/weight_artifact.py)
