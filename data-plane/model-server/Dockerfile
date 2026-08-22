# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root so Cargo uses the data-plane workspace and
# its engine adapters. The inference engine image must provide the Python
# entrypoint required by the selected adapter; the current vLLM adapter uses
# vllm.entrypoints.cli.main.

ARG RUST_BUILDER_IMAGE=docker.io/library/rust:1.97.1-slim-bullseye
ARG INFERENCE_ENGINE_IMAGE=ghcr.io/shiweijiezero/foretoken/model-server:0.0.1

FROM ${RUST_BUILDER_IMAGE} AS builder

WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential perl pkg-config \
    && rm -rf /var/lib/apt/lists/*

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

LABEL org.opencontainers.image.source="https://github.com/shiweijiezero/foretoken"

COPY --from=builder /out/foretoken-model-server /usr/local/bin/foretoken-model-server

# Preserve the inference engine image's runtime user and environment. The workload owns its
# security context, cache environment, and writable volume mounts.
ENTRYPOINT ["foretoken-model-server"]
