# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root so Cargo uses the data-plane workspace and
# its engine adapters. The inference engine image must provide the Python
# entrypoint required by the selected adapter. FORETOKEN_VLLM_PYTHON may select
# an image-specific executable; model-server otherwise uses `python`. The
# current vLLM adapter uses vllm.entrypoints.cli.main.

ARG BASE_IMAGE_REGISTRY=docker.io
ARG UV_IMAGE_REGISTRY=ghcr.io
ARG INFERENCE_ENGINE_IMAGE_REGISTRY=ghcr.io
ARG RUST_BUILDER_IMAGE=${BASE_IMAGE_REGISTRY}/library/rust:1.97.1-slim-bullseye
ARG INFERENCE_ENGINE_IMAGE=${INFERENCE_ENGINE_IMAGE_REGISTRY}/shiweijiezero/foretoken/model-server
ARG FORETOKEN_VLLM_PYTHON
ARG UV_IMAGE=${UV_IMAGE_REGISTRY}/astral-sh/uv:0.9.10

FROM ${UV_IMAGE} AS uv

# Keep the build-time patch utility out of the inference image.
FROM ${BASE_IMAGE_REGISTRY}/library/debian:bookworm-slim AS patch-tool
RUN apt-get update \
    && apt-get install -y --no-install-recommends patch ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# The upstream musl binary also runs in inference images with older glibc.
FROM patch-tool AS dragonfly-client
ARG TARGETARCH
ARG DRAGONFLY_CLIENT_VERSION=1.5.5
ARG FORETOKEN_GITHUB_MIRROR
RUN case "$TARGETARCH" in amd64) arch=x86_64 ;; arm64) arch=aarch64 ;; *) exit 1 ;; esac \
    && package="dragonfly-client-v${DRAGONFLY_CLIENT_VERSION}-${arch}-unknown-linux-musl" \
    && origin="${FORETOKEN_GITHUB_MIRROR:-https://github.com}" \
    && curl --fail --location "${origin%/}/dragonflyoss/client/releases/download/v${DRAGONFLY_CLIENT_VERSION}/${package}.tar.gz" --output /tmp/client.tar.gz \
    && mkdir /out \
    && tar -xzf /tmp/client.tar.gz --strip-components=1 -C /out "$package/dfget" "$package/LICENSE" \
    && chmod 755 /out/dfget

FROM ${RUST_BUILDER_IMAGE} AS builder

ARG FORETOKEN_GITHUB_MIRROR
ARG FORETOKEN_CARGO_REGISTRY
ARG CARGO_NET_GIT_FETCH_WITH_CLI
ARG FORETOKEN_MODEL_SERVER_BINARY=foretoken-model-server

RUN if [ "${CARGO_NET_GIT_FETCH_WITH_CLI}" = true ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends git \
      && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /workspace

COPY data-plane/ ./data-plane/

# Bullseye cannot load procedural macros linked against the frontend builder's newer glibc.
RUN --mount=type=cache,id=foretoken-cargo-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=foretoken-cargo-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=foretoken-cargo-target-model-server,target=/workspace/data-plane/target,sharing=locked \
    if [ -n "${FORETOKEN_GITHUB_MIRROR}" ]; then \
      printf '[url "%s/"]\n\tinsteadOf = https://github.com/\n' \
        "${FORETOKEN_GITHUB_MIRROR%/}" > /root/.gitconfig; \
    fi \
    && if [ -n "${FORETOKEN_CARGO_REGISTRY}" ]; then \
      mkdir -p /workspace/.cargo; \
      printf '[source.crates-io]\nreplace-with = "foretoken-mirror"\n\n[source.foretoken-mirror]\nregistry = "%s"\n' \
        "${FORETOKEN_CARGO_REGISTRY}" > /workspace/.cargo/config.toml; \
    fi \
    && if [ -n "${CARGO_NET_GIT_FETCH_WITH_CLI}" ]; then \
      export CARGO_NET_GIT_FETCH_WITH_CLI; \
    fi \
    && cargo build --locked --release \
      --manifest-path data-plane/Cargo.toml \
      --package foretoken-model-server --bin "${FORETOKEN_MODEL_SERVER_BINARY}" \
    && install -Dm755 \
      "data-plane/target/release/${FORETOKEN_MODEL_SERVER_BINARY}" \
      /out/model-server

# vLLM-Omni images already contain their Python runtime and model plugins. This
# target only adds the thin Foretoken lifecycle/proxy adapter and deliberately
# skips the standard vLLM patch and dependency stages below.
FROM ${INFERENCE_ENGINE_IMAGE} AS omni-runtime

ARG OMNI_VIDEO_SYNC_TIMEOUT=4000
ENV VLLM_OMNI_VIDEO_SYNC_TIMEOUT=${OMNI_VIDEO_SYNC_TIMEOUT} \
    VLLM_OMNI_DLO_DP_WAVE_TIMEOUT=${OMNI_VIDEO_SYNC_TIMEOUT}

ARG OCI_SOURCE=https://github.com/shiweijiezero/foretoken
ARG OCI_REVISION
LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"
COPY --from=builder /out/model-server /usr/local/bin/foretoken-omni-model-server
ENTRYPOINT ["foretoken-omni-model-server"]

FROM ${INFERENCE_ENGINE_IMAGE} AS engine-runtime

ARG FORETOKEN_VLLM_PYTHON
ENV FORETOKEN_VLLM_PYTHON=${FORETOKEN_VLLM_PYTHON}

# Patch the installed Python engine, not the independent Rust build source.
# A derived Foretoken image may already contain this exact backport.
RUN --mount=from=patch-tool,source=/usr/bin/patch,target=/usr/local/bin/patch \
    --mount=type=bind,source=data-plane/patches/vllm/vllm-python-profiling.patch,target=/tmp/vllm-profiling.patch \
    --mount=type=bind,source=data-plane/patches/vllm/vllm-offloading-event-identity.patch,target=/tmp/vllm-offload-identity.patch \
    --mount=type=bind,source=data-plane/patches/vllm/vllm-ready-logprobs.patch,target=/tmp/vllm-ready-logprobs.patch \
    --mount=type=bind,source=data-plane/patches/vllm/vllm-0.26-ready-logprobs.patch,target=/tmp/vllm-0.26-ready-logprobs.patch \
    vllm_site="$("${FORETOKEN_VLLM_PYTHON:-python}" -c \
      'from importlib.metadata import distribution; print(distribution("vllm").locate_file(""))')" \
    && vllm_version="$("${FORETOKEN_VLLM_PYTHON:-python}" -c 'from importlib.metadata import version; print(version("vllm"))')" \
    && ready_patch=/tmp/vllm-ready-logprobs.patch \
    && offload_patch= \
    && case "$vllm_version" in \
         0.26.*) ready_patch=/tmp/vllm-0.26-ready-logprobs.patch ;; \
         0.30.*) offload_patch=/tmp/vllm-offload-identity.patch ;; \
       esac \
    && offload_events="$vllm_site/vllm/distributed/kv_transfer/kv_connector/v1/offloading/events.py" \
    && if [ ! -f "$offload_events" ]; then offload_patch=; fi \
    && for engine_patch in /tmp/vllm-profiling.patch "$ready_patch" "$offload_patch"; do \
         [ -n "$engine_patch" ] || continue; \
         if patch --force --reverse --dry-run --fuzz=0 --strip=1 \
              --directory="$vllm_site" --input="$engine_patch" >/dev/null 2>&1; then \
           printf 'vLLM patch already installed: %s\n' "${engine_patch##*/}"; \
         else \
           patch --batch --forward --fuzz=0 --no-backup-if-mismatch --strip=1 \
             --directory="$vllm_site" --input="$engine_patch" || exit 1; \
         fi; \
       done \
    && "${FORETOKEN_VLLM_PYTHON:-python}" -m compileall -q -f \
      "$vllm_site/vllm/profiler/wrapper.py" "$vllm_site/vllm/v1/worker/gpu_worker.py" \
      "$vllm_site/vllm/v1/engine/__init__.py" "$vllm_site/vllm/v1/engine/core.py" \
    && if [ -n "$offload_patch" ]; then \
         "${FORETOKEN_VLLM_PYTHON:-python}" -m compileall -q -f "$offload_events"; \
       fi

ARG UV_DEFAULT_INDEX=https://pypi.org/simple
# Engine images may provide a Python virtual environment without pip.
# MACA also exposes torch.version.cuda, so select dependencies from the engine build.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    python_path=$("${FORETOKEN_VLLM_PYTHON:-python}" -c \
      'import sys; print(sys.executable)') \
    && backend=$("$python_path" -c \
      'import torch; print("maca" if getattr(torch.version, "maca", None) else "cuda" if torch.version.cuda else "other")') \
    && set -- modelscope==1.35.3 tzdata \
    && case "$backend" in cuda) set -- "$@" bitsandbytes==0.49.2 modelexpress==0.6.0 nixl==1.4.1 ;; esac \
    && "$python_path" -c 'from importlib.metadata import version; print("torch==" + version("torch")); print("vllm==" + version("vllm"))' > /tmp/foretoken-engine-constraints.txt \
    && uv pip install --no-cache --python "$python_path" \
      --constraint /tmp/foretoken-engine-constraints.txt \
      --default-index "${UV_DEFAULT_INDEX}" "$@" \
    && uv pip check --python "$python_path" \
    && rm /tmp/foretoken-engine-constraints.txt

FROM engine-runtime AS runtime

ARG OCI_SOURCE=https://github.com/shiweijiezero/foretoken
ARG OCI_REVISION
LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"
COPY --from=builder /out/model-server /usr/local/bin/foretoken-model-server
COPY --from=dragonfly-client /out/dfget /usr/local/bin/dfget
COPY --from=dragonfly-client /out/LICENSE /usr/share/licenses/dragonfly-client/LICENSE
COPY data-plane/model-server/python/ /opt/foretoken/python/

# Preserve the inference engine image's runtime user and environment. The workload owns its
# security context, cache environment, and writable volume mounts.
ENTRYPOINT ["foretoken-model-server"]
