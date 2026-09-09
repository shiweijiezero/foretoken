# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root so Cargo uses the data-plane workspace and
# its engine adapters. The inference engine image must provide the Python
# entrypoint required by the selected adapter. FORETOKEN_VLLM_PYTHON may select
# an image-specific executable; model-server otherwise uses `python`. The
# current vLLM adapter uses vllm.entrypoints.cli.main.

ARG RUST_BUILDER_IMAGE=docker.io/library/rust:1.97.1-slim-bullseye@sha256:b015aa055657c177e783b690c79b0517cda38fae1fa677b133a954d61f4afdc3
ARG INFERENCE_ENGINE_IMAGE=ghcr.io/shiweijiezero/foretoken/model-server:0.0.1
ARG FORETOKEN_VLLM_PYTHON
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.10

FROM ${UV_IMAGE} AS uv
FROM ${RUST_BUILDER_IMAGE} AS builder

WORKDIR /workspace

COPY data-plane/ ./data-plane/

RUN --mount=type=cache,id=foretoken-cargo-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=foretoken-cargo-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=foretoken-cargo-target,target=/workspace/data-plane/target,sharing=locked \
    cargo build --locked --release \
      --manifest-path data-plane/Cargo.toml \
      --package foretoken-model-server \
    && install -Dm755 \
      data-plane/target/release/foretoken-model-server \
      /out/foretoken-model-server

FROM ${INFERENCE_ENGINE_IMAGE}

ARG FORETOKEN_VLLM_PYTHON
ENV FORETOKEN_VLLM_PYTHON=${FORETOKEN_VLLM_PYTHON}
ARG PYPI_INDEX_URL=https://pypi.org/simple
# Engine images may provide a Python virtual environment without pip.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv pip install --no-cache --python "${FORETOKEN_VLLM_PYTHON:-python}" \
      --index-url "${PYPI_INDEX_URL}" modelscope==1.35.3

LABEL org.opencontainers.image.source="https://github.com/shiweijiezero/foretoken"

COPY --from=builder /out/foretoken-model-server /usr/local/bin/foretoken-model-server

# Preserve the inference engine image's runtime user and environment. The workload owns its
# security context, cache environment, and writable volume mounts.
ENTRYPOINT ["foretoken-model-server"]
