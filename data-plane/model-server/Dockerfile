# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root so Cargo uses the data-plane workspace and
# its official vLLM submodule. The runtime image must contain python3 and
# vllm.entrypoints.cli.main.

ARG RUST_BUILDER_IMAGE=docker.io/library/rust:1.97.1-slim-bullseye
ARG VLLM_RUNTIME_IMAGE

FROM ${RUST_BUILDER_IMAGE} AS builder

WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential perl pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY data-plane/ ./data-plane/

RUN cargo build --locked --release \
    --manifest-path data-plane/Cargo.toml \
    --package foretoken-model-server

FROM ${VLLM_RUNTIME_IMAGE}

COPY --from=builder /workspace/data-plane/target/release/foretoken-model-server /usr/local/bin/foretoken-model-server

ENV HOME=/var/cache/foretoken \
    HF_HOME=/var/cache/foretoken/huggingface \
    XDG_CACHE_HOME=/var/cache/foretoken/xdg

RUN mkdir -p /var/cache/foretoken && chown -R 65532:65532 /var/cache/foretoken

USER 65532:65532
ENTRYPOINT ["foretoken-model-server"]
