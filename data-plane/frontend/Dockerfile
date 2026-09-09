# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root so Cargo uses the data-plane workspace and
# its official vLLM submodule.

ARG RUST_BUILDER_IMAGE=docker.io/library/rust:1.97.1-slim-bookworm@sha256:2775a09d208ff0d7c1f50490c45b62db929e87ba1dcbc3f2132ac71a704bcdd3
ARG RUNTIME_IMAGE=docker.io/library/debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

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
    && apt-get install -y --no-install-recommends ca-certificates libssl3 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 65532 --create-home foretoken \
    && mkdir -p /var/cache/foretoken \
    && chown -R 65532:65532 /var/cache/foretoken

COPY --from=builder /out/foretoken-frontend /usr/local/bin/foretoken-frontend

ENV HF_HOME=/var/cache/foretoken/huggingface

USER 65532:65532
ENTRYPOINT ["foretoken-frontend"]
