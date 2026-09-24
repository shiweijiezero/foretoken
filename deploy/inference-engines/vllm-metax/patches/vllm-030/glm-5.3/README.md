# GLM-5.3 on the MetaX source runtime

The MetaX engine build applies this bundle automatically to the core and plugin revisions in [`source-environment.json`](source-environment.json). The manifest also defines patch targets and application order.

The shared [compatibility patch](../metax-compatibility.patch) adapts the plugin's imports and dependencies to the core and preserves the core's automatic model-runner selection. The GLM patches cover typed KV layouts, sparse attention, sequence-parallel layers, MTP, and mHC normalization.

Source patches are applied before building the engine wheels. The DeepGEMM patch is applied after dependency installation because it modifies the installed kernel package. Update the source pair and its patches together.

MTP retains the complete sparse indexer and separate cache groups. The core selects Model Runner V2 when the execution configuration supports it, including the GLM-5.3 BF16 recipe.
