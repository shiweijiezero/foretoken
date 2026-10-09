# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

ARG BASE_IMAGE=vllm/vllm-openai:v0.30.0
ARG VLLM_OMNI_REVISION=ad025defe68a46bdc3590c53161889aa6932ff4c

FROM docker.io/library/debian:bookworm-slim AS source
ARG VLLM_OMNI_REVISION
ARG FORETOKEN_GITHUB_MIRROR=https://github.com
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src/vllm-omni
RUN git init && git remote add origin "${FORETOKEN_GITHUB_MIRROR%/}/vllm-project/vllm-omni.git" \
    && git fetch --depth=1 origin "${VLLM_OMNI_REVISION}" \
    && git checkout --detach FETCH_HEAD

FROM ${BASE_IMAGE}
ARG VLLM_OMNI_REVISION
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
LABEL org.opencontainers.image.source="https://github.com/vllm-project/vllm-omni" \
      org.opencontainers.image.revision="${VLLM_OMNI_REVISION}"
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git jq \
    && rm -rf /var/lib/apt/lists/*
COPY --from=source /src/vllm-omni /app/vllm-omni
RUN uv pip install --python "$(python3 -c 'import sys; print(sys.executable)')" \
    --no-cache-dir --default-index "${UV_DEFAULT_INDEX}" \
    /app/vllm-omni modelscope==1.35.3
ENTRYPOINT []
