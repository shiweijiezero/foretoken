# syntax=docker/dockerfile:1.19
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root using the declared data-plane dependencies.

ARG BASE_IMAGE_REGISTRY=docker.io
ARG UV_IMAGE_REGISTRY=ghcr.io
ARG UV_IMAGE=${UV_IMAGE_REGISTRY}/astral-sh/uv:0.12.22
ARG RUST_BUILDER_IMAGE=${BASE_IMAGE_REGISTRY}/library/rust:1.97.1-slim-bookworm
ARG RUNTIME_IMAGE=${BASE_IMAGE_REGISTRY}/library/debian:bookworm-slim

FROM ${UV_IMAGE} AS uv

FROM ${RUST_BUILDER_IMAGE} AS compiler
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git libssl-dev perl pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/foretoken-python
ARG FORETOKEN_GITHUB_MIRROR
RUN mirror="${FORETOKEN_GITHUB_MIRROR:-https://github.com}"; \
    UV_PYTHON_INSTALL_MIRROR="${mirror%/}/astral-sh/python-build-standalone/releases/download" \
      uv venv /opt/foretoken-compiler --python 3.12
ENV PATH=/opt/foretoken-compiler/bin:${PATH}
WORKDIR /workspace

FROM compiler AS builder
ARG FORETOKEN_GITHUB_MIRROR
ARG FORETOKEN_CARGO_REGISTRY
ARG CARGO_NET_GIT_FETCH_WITH_CLI
COPY data-plane/ ./data-plane/
COPY deploy/build-source ./deploy/build-source
ENV CARGO_HOME=/var/cache/foretoken/cargo-home
RUN --mount=type=secret,id=foretoken-build-jobs,env=BUILD_JOBS \
    --mount=type=cache,id=foretoken-vllm-source,target=/var/cache/foretoken/vllm \
    --mount=type=cache,id=foretoken-cargo-home,target=/var/cache/foretoken/cargo-home \
    --mount=type=cache,id=foretoken-cargo-target-bookworm-prepared-source,target=/workspace/data-plane/target,sharing=locked \
    python3 -I deploy/build-source frontend /out --cache /var/cache/foretoken --parts rust

FROM scratch AS source-export
COPY --from=builder /out/ /

FROM ${RUNTIME_IMAGE} AS environment
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 65532 --create-home foretoken \
    && mkdir -p /var/cache/foretoken \
    && chown -R 65532:65532 /var/cache/foretoken
ENV HF_HOME=/var/cache/foretoken/huggingface
USER 65532:65532

ARG OCI_SOURCE=https://github.com/shiweijiezero/foretoken
ARG OCI_REVISION
LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"

FROM environment AS runtime
COPY --from=builder /out/bin/foretoken-frontend /usr/local/bin/foretoken-frontend
ENTRYPOINT ["foretoken-frontend"]
