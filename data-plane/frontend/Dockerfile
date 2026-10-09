# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root so Cargo uses the data-plane workspace and
# its official vLLM submodule.

ARG BASE_IMAGE_REGISTRY=docker.io
ARG RUST_BUILDER_IMAGE=${BASE_IMAGE_REGISTRY}/library/rust:1.97.1-slim-bookworm
ARG RUNTIME_IMAGE=${BASE_IMAGE_REGISTRY}/library/debian:bookworm-slim

FROM ${RUST_BUILDER_IMAGE} AS builder

WORKDIR /workspace

ARG FORETOKEN_GITHUB_MIRROR
ARG FORETOKEN_CARGO_REGISTRY
ARG CARGO_NET_GIT_FETCH_WITH_CLI
ARG VLLM_REVISION

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git libssl-dev perl pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY data-plane/ ./data-plane/

# Key the target cache by the builder image: artifacts built on different
# Debian releases are not interchangeable even with the same Rust version.
# Retain prepared Rust inputs with their outputs, isolated from legacy builders.
# Copy changed trees afresh so patch removals also receive mtimes Cargo can observe.
RUN --mount=type=cache,id=foretoken-vllm-${VLLM_REVISION},target=/var/cache/vllm,sharing=locked \
    --mount=type=cache,id=foretoken-cargo-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=foretoken-cargo-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=foretoken-cargo-target-bookworm-prepared-source,target=/workspace/data-plane/target,sharing=locked \
    if [ -n "${FORETOKEN_GITHUB_MIRROR}" ]; then \
      git config --global url."${FORETOKEN_GITHUB_MIRROR%/}/".insteadOf https://github.com/; \
    fi \
    && if [ -n "${FORETOKEN_CARGO_REGISTRY}" ]; then \
      mkdir -p /workspace/.cargo; \
      printf '[source.crates-io]\nreplace-with = "foretoken-mirror"\n\n[source.foretoken-mirror]\nregistry = "%s"\n' \
        "${FORETOKEN_CARGO_REGISTRY}" > /workspace/.cargo/config.toml; \
    fi \
    && if [ -n "${CARGO_NET_GIT_FETCH_WITH_CLI}" ]; then \
      export CARGO_NET_GIT_FETCH_WITH_CLI; \
    fi \
    && if [ ! -f data-plane/third_party/vllm/vllm/v1/engine/core.py ]; then \
      if [ ! -f /var/cache/vllm/rust/Cargo.toml ]; then \
        git -C /var/cache/vllm init -q \
        && git -C /var/cache/vllm fetch --depth=1 https://github.com/vllm-project/vllm.git "${VLLM_REVISION}" \
        && git -C /var/cache/vllm checkout -q --detach FETCH_HEAD; \
      fi \
      && mkdir -p data-plane/third_party/vllm \
      && git -C /var/cache/vllm archive HEAD | tar -x -C data-plane/third_party/vllm; \
    fi \
    && cargo run --locked --manifest-path data-plane/xtask/Cargo.toml -- prepare-vllm \
    && mkdir -p data-plane/target/vllm-rust \
    && if git diff --no-index --quiet data-plane/target/vllm-rust data-plane/third_party/vllm/rust; then \
      :; \
    else \
      result=$?; \
      test "$result" -eq 1 \
      && rm -rf data-plane/target/vllm-rust \
      && cp -R data-plane/third_party/vllm/rust data-plane/target/vllm-rust; \
    fi \
    && rm -rf data-plane/third_party/vllm/rust \
    && cp -a data-plane/target/vllm-rust data-plane/third_party/vllm/rust \
    && cargo build --locked --release \
      --manifest-path data-plane/Cargo.toml \
      --package foretoken-frontend \
    && install -Dm755 \
      data-plane/target/release/foretoken-frontend \
      /out/foretoken-frontend

FROM scratch AS source-export
COPY --from=builder /out/foretoken-frontend /bin/foretoken-frontend
COPY <<EOF /complete.json
{"component":"frontend"}
EOF

FROM ${RUNTIME_IMAGE} AS environment

ARG OCI_SOURCE=https://github.com/shiweijiezero/foretoken
ARG OCI_REVISION
LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 65532 --create-home foretoken \
    && mkdir -p /var/cache/foretoken \
    && chown -R 65532:65532 /var/cache/foretoken

ENV HF_HOME=/var/cache/foretoken/huggingface
USER 65532:65532

FROM environment AS runtime
COPY --from=builder /out/foretoken-frontend /usr/local/bin/foretoken-frontend
ENTRYPOINT ["foretoken-frontend"]
