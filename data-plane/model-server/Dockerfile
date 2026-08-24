# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root so Cargo uses the data-plane workspace and
# its engine adapters. ENGINE_FEATURES selects the inference backend compiled
# into the binary (backend-vllm or backend-sglang), and INFERENCE_ENGINE_IMAGE
# must provide the Python entrypoint required by that adapter.

ARG RUST_BUILDER_IMAGE=docker.io/library/rust:1.97.1-slim-bullseye
ARG INFERENCE_ENGINE_IMAGE

FROM ${RUST_BUILDER_IMAGE} AS builder

ARG ENGINE_FEATURES=backend-vllm

WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential perl pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY data-plane/ ./data-plane/

RUN cargo build --locked --release \
    --no-default-features \
    --features ${ENGINE_FEATURES} \
    --manifest-path data-plane/Cargo.toml \
    --package foretoken-model-server

FROM ${INFERENCE_ENGINE_IMAGE}

COPY --from=builder /workspace/data-plane/target/release/foretoken-model-server /usr/local/bin/foretoken-model-server

# Preserve the inference engine image's runtime user and environment. The workload owns its
# security context, cache environment, and writable volume mounts.
ENTRYPOINT ["foretoken-model-server"]
