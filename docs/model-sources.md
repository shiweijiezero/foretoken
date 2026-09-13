<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Model sources

[中文](model-sources_zh.md)

Each `ModelService` selects the source for its model, tokenizer, config, and chat template. Hugging Face Hub is the default.

```yaml
spec:
  model: Qwen/Qwen3-0.6B
  source: hf # Supported sources: local, hf, modelscope. Defaults to hf.
```

Use `source: modelscope` with the same model identifier to load it from ModelScope. Different model services behind one frontend may use different sources.

For a Hugging Face-compatible endpoint, set the platform access configuration and install with that values file:

```yaml
runtime:
  vllm:
    modelSource:
      endpoint: https://hub.example.com
```

```bash
foretoken install --values model-source-values.yaml
```

## Use a local model directory

Place a complete model below the configured model root:

```text
examples/quickstart/data/models/checkpointA/A3/
```

Select the local source and keep the relative identifier as the public model name:

```yaml
spec:
  model: checkpointA/A3
  source: local
```

An absolute directory already mounted in both frontend and model-server Pods is also supported. A missing local directory or a failed remote download stops that model from becoming ready; Foretoken does not switch sources.

Deploy the configuration normally:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

See [Model storage](model-storage.md) for directory-backed and PVC-backed storage.
