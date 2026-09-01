<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Single-Model Quick Start

[English](README.md) | [中文](README_zh.md)

For two models with autoscaling, see [Multi-Model Quick Start](../multi-model-quickstart/README.md).

This example deploys one frontend and one `Qwen/Qwen3-0.6B` model replica using one GPU.

## Deploy

Install the Foretoken platform first. Then install the CLI from the repository root with pip:

```bash
pip install -e .
```

Or create and activate a virtual environment with uv:

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

Deploy the example:

```bash
foretoken deploy examples/quickstart
```

The command reports each service state as it changes and exits when the current configuration is ready.

## Send a request

Resolve the frontend URL:

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
```

Send an OpenAI-compatible request:

```bash
curl "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Reply with: Foretoken is ready"}],
    "max_tokens": 32,
    "temperature": 0
  }'
printf '\n'
```

## Clean up

```bash
foretoken delete examples/quickstart
```
