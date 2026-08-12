# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root. The runtime artifact must contain python3 and
# vllm.entrypoints.cli.main built from vLLM commit 5b14019576475224d86044b262e28a04a85d4086.

ARG RUST_BUILDER_IMAGE=docker.io/library/rust:1.97.1-slim-bullseye
ARG VLLM_RUNTIME_IMAGE

FROM ${RUST_BUILDER_IMAGE} AS builder
WORKDIR /workspace
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential perl pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY third_party/vllm-rust/source/rust/ ./third_party/vllm-rust/source/rust/
COPY data-plane/model-protocol/ ./data-plane/model-protocol/
COPY data-plane/model-server/ ./data-plane/model-server/
RUN cargo build --locked --release --manifest-path data-plane/model-server/Cargo.toml

FROM ${VLLM_RUNTIME_IMAGE}
COPY --from=builder /workspace/data-plane/model-server/target/release/foretoken-model-server /usr/local/bin/foretoken-model-server
ENV HOME=/var/cache/foretoken \
    HF_HOME=/var/cache/foretoken/huggingface \
    XDG_CACHE_HOME=/var/cache/foretoken/xdg
RUN mkdir -p /var/cache/foretoken && chown -R 65532:65532 /var/cache/foretoken
USER 65532:65532
ENTRYPOINT ["foretoken-model-server"]
