# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root so Cargo uses the data-plane workspace and
# its official vLLM submodule.

ARG RUST_BUILDER_IMAGE=docker.io/library/rust:1.97.1-slim-bullseye
ARG RUNTIME_IMAGE=docker.io/library/debian:bullseye-slim

FROM ${RUST_BUILDER_IMAGE} AS builder

WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libssl-dev perl pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY data-plane/ ./data-plane/

RUN --mount=type=cache,id=foretoken-cargo-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=foretoken-cargo-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=foretoken-cargo-target,target=/workspace/data-plane/target,sharing=locked \
    cargo build --locked --release \
      --manifest-path data-plane/Cargo.toml \
      --package foretoken-frontend \
    && install -Dm755 \
      data-plane/target/release/foretoken-frontend \
      /out/foretoken-frontend

FROM ${RUNTIME_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/shiweijiezero/foretoken"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libssl1.1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 65532 --create-home foretoken \
    && mkdir -p /var/cache/foretoken \
    && chown -R 65532:65532 /var/cache/foretoken

COPY --from=builder /out/foretoken-frontend /usr/local/bin/foretoken-frontend

ENV HF_HOME=/var/cache/foretoken/huggingface

USER 65532:65532
ENTRYPOINT ["foretoken-frontend"]
