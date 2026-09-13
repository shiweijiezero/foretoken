<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Model sources

[中文](model-sources_zh.md)

Foretoken loads model weights and frontend tokenizer, config, and chat-template files from the same source. Hugging Face Hub is used when no model-source values are set, so the default path remains `foretoken install`.

## Select a remote source

For a Hugging Face-compatible endpoint, create `model-source-values.yaml`:

```yaml
runtime:
  vllm:
    modelSource:
      endpoint: https://hub.example.com
```

For ModelScope, use:

```yaml
runtime:
  vllm:
    modelSource:
      provider: modelscope
```

Install the platform with the selected values:

```bash
foretoken install --values model-source-values.yaml
```

`endpoint` is only valid for the default Hugging Face provider. Repository or download errors stop model startup instead of switching sources.

Deploy the maintained example with the same public model identifier:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

## Use a local model directory

A complete directory below the configured model root takes precedence over remote sources. Place the files below the example data directory:

```text
examples/quickstart/data/models/checkpointA/A3/
```

Use the same relative identifier in `ModelService`:

```yaml
spec:
  model: checkpointA/A3
```

Deploy with the command above. The model server and frontend reuse the directory without changing the public model identifier. See [Model storage](model-storage.md) for directory-backed and PVC-backed storage.
