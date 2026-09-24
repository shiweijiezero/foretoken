# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

FROM docker.io/library/debian:bookworm-slim AS patch-tool
RUN apt-get update \
    && apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/*

# NVIDIA runtime with encoder-only execution and Mooncake P/D transfer.
FROM vllm/vllm-openai@sha256:4cbfd34aac145fd1870381c030131c7f868fcad45448f401ecdb5fd4ed020b42
RUN python3 -m pip install --no-cache-dir --only-binary=:all: \
        mooncake-transfer-engine==0.3.12.post1 nvidia-cuda-runtime-cu12 \
    && ln -s /usr/bin/python3 /usr/local/bin/python

RUN --mount=from=patch-tool,source=/usr/bin/patch,target=/usr/local/bin/patch \
    --mount=type=bind,source=data-plane/patches/vllm-mooncake-context-parallel.patch,target=/tmp/vllm-mooncake-context-parallel.patch \
    vllm_site="$(python3 -c \
      'from importlib.metadata import distribution; print(distribution("vllm").locate_file(""))')" \
    && patch --batch --forward --fuzz=0 --no-backup-if-mismatch --strip=1 \
         --directory="$vllm_site" --input=/tmp/vllm-mooncake-context-parallel.patch \
    && python3 -m compileall -q -f \
      "$vllm_site/vllm/distributed/kv_transfer/kv_connector/v1/mooncake/mooncake_connector.py" \
      "$vllm_site/vllm/distributed/kv_transfer/kv_connector/v1/mooncake/mooncake_utils.py"

ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/nvidia/cuda_runtime/lib:${LD_LIBRARY_PATH}
