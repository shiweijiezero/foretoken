# GLM-5.3 on the MetaX source runtime

English | [简体中文](README_zh.md)

The MetaX engine build applies this bundle automatically to the core and plugin revisions in [`source-environment.json`](../../../source-environment.json). The manifest also defines patch targets and application order.

The shared [compatibility patch](../metax-compatibility.patch) adapts the plugin's imports and dependencies to the core and preserves the core's automatic model-runner selection. The GLM patches cover typed KV layouts, sparse attention, sequence-parallel layers, MTP, and mHC normalization.

For compressed-tensors checkpoints, the MLA patch gives projection layers their quantization configuration and leaves quantized weights with their own loaders. The INT8 MoE patch passes expert-parallel filtering to the native GEMM so blocks mapped to nonlocal experts are skipped.

Source patches are applied before building the engine wheels. The DeepGEMM patch is applied after dependency installation because it modifies the installed kernel package. Update the source pair and its patches together.

MTP retains the complete sparse indexer and separate cache groups. The core selects Model Runner V2 when the execution configuration supports it, including the GLM-5.3 BF16 recipe.

MetaX compiles the upstream PyTorch arithmetic for mHC residual-stream mixing. It preserves FP32 mixing, intermediate BF16 rounding, the configured Sinkhorn iterations, and input normalization; CUDA Graph capture remains with the model runner. The core normalization patch also keeps HIP fallbacks from normalizing twice. Related upstream work: [vLLM #56856](https://github.com/vllm-project/vllm/pull/56856).

The MetaX KDA adapter selects an eight-warp, three-stage recompute launch. The two-stage configuration can corrupt the W transform and produce non-finite recurrent state during long prefills; timing-based autotuning does not detect this numerical failure.

Dense DFlash/DSpark proposals are omitted from runtime DP dummy batches because they have no output consumer or cross-DP draft collective. Target synchronization and draft execution for profiling, warmup, and graph capture are retained.
