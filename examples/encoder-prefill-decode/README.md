<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Separate image encoding, prefill and decoding

English | [简体中文](README_zh.md)

Serve `Qwen/Qwen2.5-VL-3B-Instruct` with separate Encoder, Prefill and Decode Pools. The example requests four GPUs: one for encoding, one for prefill and two for tensor-parallel decoding. Clients continue using the ordinary chat-completion endpoint.

## Platform preparation

This example uses a source installation and four NVIDIA GPUs. From the repository root, build its inference-engine base:

```bash
docker build -f examples/encoder-prefill-decode/runtime.Dockerfile -t foretoken-vllm:epd .
```

The cluster needs a StorageClass that supports `ReadWriteMany`. Set `storageClassName` in `encoder-cache.yaml` if the default StorageClass does not provide shared storage. RDMA transport also needs allocated RDMA devices with GPUDirect RDMA enabled; choose TCP below if these are unavailable.

Save the runtime choice and shared encoder cache settings as `platform-values.yaml`:

```yaml
runtime:
  vllm:
    image: foretoken-vllm:epd
    ec:
      sharedStorageClaim: encoder-cache
```

RDMA is the default P/D transport. To use Mooncake TCP instead, add this section under the same `runtime.vllm` mapping:

```yaml
runtime:
  vllm:
    pd:
      protocol: tcp
```

TCP does not require RDMA devices; GPU KV data is staged through host memory.

Install from this checkout:

```bash
foretoken install -e . --values platform-values.yaml
```

For a remote cluster, also pass `--registry REGISTRY` with a container registry accessible to its nodes. See the [CLI guide](../../cli/README.md#current-source) for installation options.

## Deploy

From the repository root:

```bash
foretoken deploy examples/encoder-prefill-decode --timeout 20m
export FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/encoder-prefill-decode)"
```

## Ask about an image

Save a JPEG as `image.jpg`, then send it as a base64 data URL:

```bash
python - <<'PY'
import base64
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

image = base64.b64encode(Path("image.jpg").read_bytes()).decode()
body = {
    "model": "Qwen/Qwen2.5-VL-3B-Instruct",
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Describe this image."},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
    ]}],
    "max_tokens": 128,
}
request = Request(
    os.environ["FORETOKEN_FRONTEND_URL"] + "/v1/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
)
with urlopen(request, timeout=300) as response:
    print(json.load(response)["choices"][0]["message"]["content"])
PY
```

## Adjust capacity

Each stage has one Pool; its `replicas`, resource requests and `engineArgs` can be adjusted independently. A Pool-level `engineArgs` replaces the service-level dictionary; the YAML anchor preserves shared settings in the example. Match GPU requests to the chosen parallelism as described in [Inference parameters](../../docs/inference-parameters.md).

Reapply the directory with `foretoken deploy` after editing it. Use `foretoken status examples/encoder-prefill-decode` to inspect readiness. Encoder output travels through the shared volume, while Prefill transfers KV cache to Decode over the platform-selected Mooncake transport; clients do not supply transfer addresses or intermediate results.

## Clean up

```bash
foretoken delete examples/encoder-prefill-decode
```

This deletes the example namespace and cache claim. Encoder files are shared cache entries rather than per-request temporary files; the volume's reclaim policy determines whether their storage is retained. The shared Foretoken platform remains installed.
