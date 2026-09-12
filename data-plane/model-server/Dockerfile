# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root so Cargo uses the data-plane workspace and
# its engine adapters. The inference engine image must provide the Python
# entrypoint required by the selected adapter. FORETOKEN_VLLM_PYTHON may select
# an image-specific executable; model-server otherwise uses `python`. The
# current vLLM adapter uses vllm.entrypoints.cli.main.

ARG RUST_BUILDER_IMAGE=docker.io/library/rust:1.97.1-slim-bullseye
ARG INFERENCE_ENGINE_IMAGE=ghcr.io/shiweijiezero/foretoken/model-server:0.0.2
ARG FORETOKEN_VLLM_PYTHON
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.10

FROM ${UV_IMAGE} AS uv

# Keep the build-time patch utility out of the inference image.
FROM docker.io/library/debian:bookworm-slim AS patch-tool
RUN apt-get update \
    && apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/*

FROM ${RUST_BUILDER_IMAGE} AS builder

WORKDIR /workspace

COPY data-plane/ ./data-plane/

# Bullseye cannot load procedural macros linked against the frontend builder's newer glibc.
RUN --mount=type=cache,id=foretoken-cargo-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=foretoken-cargo-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=foretoken-cargo-target-model-server,target=/workspace/data-plane/target,sharing=locked \
    cargo build --locked --release \
      --manifest-path data-plane/Cargo.toml \
      --package foretoken-model-server \
    && install -Dm755 \
      data-plane/target/release/foretoken-model-server \
      /out/foretoken-model-server

FROM ${INFERENCE_ENGINE_IMAGE} AS engine-runtime

ARG FORETOKEN_VLLM_PYTHON
ENV FORETOKEN_VLLM_PYTHON=${FORETOKEN_VLLM_PYTHON}

# Patch the installed Python engine, not the independent Rust build source.
# A derived Foretoken image may already contain this exact backport.
RUN --mount=from=patch-tool,source=/usr/bin/patch,target=/usr/local/bin/patch \
    --mount=type=bind,source=data-plane/patches/vllm-python-profiling.patch,target=/tmp/vllm-profiling.patch \
    vllm_site="$("${FORETOKEN_VLLM_PYTHON:-python}" -c \
      'from importlib.metadata import distribution; print(distribution("vllm").locate_file(""))')" \
    && if patch --force --reverse --dry-run --fuzz=0 --strip=1 \
         --directory="$vllm_site" --input=/tmp/vllm-profiling.patch >/dev/null 2>&1; then \
         printf '%s\n' 'vLLM profiling backport already installed'; \
       else \
         patch --batch --forward --fuzz=0 --no-backup-if-mismatch --strip=1 \
           --directory="$vllm_site" --input=/tmp/vllm-profiling.patch; \
       fi \
    && "${FORETOKEN_VLLM_PYTHON:-python}" -m compileall -q -f \
      "$vllm_site/vllm/profiler/wrapper.py" "$vllm_site/vllm/v1/worker/gpu_worker.py"

ARG PYPI_INDEX_URL=https://pypi.org/simple
# Engine images may provide a Python virtual environment without pip.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv pip install --no-cache --python "${FORETOKEN_VLLM_PYTHON:-python}" \
      --index-url "${PYPI_INDEX_URL}" modelscope==1.35.3

FROM engine-runtime AS runtime

LABEL org.opencontainers.image.source="https://github.com/shiweijiezero/foretoken"
COPY --from=builder /out/foretoken-model-server /usr/local/bin/foretoken-model-server

# Preserve the inference engine image's runtime user and environment. The workload owns its
# security context, cache environment, and writable volume mounts.
ENTRYPOINT ["foretoken-model-server"]
