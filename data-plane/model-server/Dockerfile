# syntax=docker/dockerfile:1.19
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root using the declared data-plane dependencies.
# The inference image supplies its Python entrypoint and accelerator libraries.

ARG BASE_IMAGE_REGISTRY=docker.io
ARG UV_IMAGE_REGISTRY=ghcr.io
ARG RUST_BUILDER_IMAGE=${BASE_IMAGE_REGISTRY}/library/rust:1.97.1-bullseye
ARG INFERENCE_ENGINE_IMAGE
ARG FORETOKEN_VLLM_PYTHON
ARG UV_IMAGE=${UV_IMAGE_REGISTRY}/astral-sh/uv:0.12.22
ARG GO_IMAGE=${BASE_IMAGE_REGISTRY}/library/golang:1.25.10-bookworm
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
ARG PREPARATION_BASE_IMAGE=${BASE_IMAGE_REGISTRY}/library/python:3.12-slim-bookworm

FROM ${UV_IMAGE} AS uv

FROM ${GO_IMAGE} AS mooncake-go

# Keep build-time patch tools out of the inference image.
FROM ${BASE_IMAGE_REGISTRY}/library/debian:bookworm-slim AS patch-tool
RUN apt-get update \
    && apt-get install -y --no-install-recommends jq patch \
    && rm -rf /var/lib/apt/lists/*

# Only Python catalog entries affect the installed engine. Project their files
# before the runtime stages so Rust-only edits retain BuildKit's environment cache.
FROM patch-tool AS runtime-patches
COPY data-plane/patches/vllm/ /input/
RUN set -eu; cd /input; mkdir /patches; \
    cp apply.py vllm_patches.py mooncake.version /patches/; \
    jq '.patches |= map(select(.python != null) | del(.rust))' catalog.json > /patches/catalog.json; \
    jq -r '.patches[].path' /patches/catalog.json | while IFS= read -r patch; do \
        cp --parents "$patch" /patches/; \
    done

FROM ${RUST_BUILDER_IMAGE} AS compiler
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/foretoken-python
ARG FORETOKEN_GITHUB_MIRROR
RUN mirror="${FORETOKEN_GITHUB_MIRROR:-https://github.com}"; \
    UV_PYTHON_INSTALL_MIRROR="${mirror%/}/astral-sh/python-build-standalone/releases/download" \
      uv venv /opt/foretoken-compiler --python 3.12
ENV PATH=/opt/foretoken-compiler/bin:${PATH}
WORKDIR /workspace/source

FROM compiler AS builder
ARG FORETOKEN_GITHUB_MIRROR
ARG FORETOKEN_CARGO_REGISTRY
ARG CARGO_NET_GIT_FETCH_WITH_CLI
ARG FORETOKEN_MODEL_SERVER_BINARY=foretoken-model-server
COPY --exclude=model-server/python data-plane/ ./data-plane/
COPY deploy/build-source ./deploy/build-source
COPY LICENSE ./LICENSE
ENV CARGO_HOME=/var/lib/foretoken/dependencies/cargo-home \
    CARGO_TARGET_DIR=/workspace/target
# Keep this target separate from the frontend's newer glibc toolchain.
RUN --mount=type=secret,id=foretoken-build-jobs,env=BUILD_JOBS \
    --mount=type=cache,id=foretoken-vllm-source,target=/var/lib/foretoken/dependencies/vllm \
    --mount=type=cache,id=foretoken-cargo-home,target=/var/lib/foretoken/dependencies/cargo-home \
    --mount=type=cache,id=foretoken-cargo-target-model-server-prepared-source,target=/workspace/target,sharing=locked \
    python3 -I deploy/build-source model-server /out --cache /var/lib/foretoken/dependencies --parts rust \
      --cache-export /compiler-cache

FROM scratch AS compiler-cache
COPY --from=builder /compiler-cache/ /

FROM scratch AS source-export
COPY --from=builder /out/ /
COPY data-plane/model-server/python/ /python/

# CPU preparation runs the same published model-server application without accelerator libraries.
FROM ${PREPARATION_BASE_IMAGE} AS preparation-environment
ARG UV_DEFAULT_INDEX
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=data-plane/model-server/provider-requirements.txt,target=/tmp/provider-requirements.txt \
    uv pip install --system --no-cache --compile-bytecode --default-index "${UV_DEFAULT_INDEX}" \
      -r /tmp/provider-requirements.txt 'huggingface_hub>=1.30' httpx requests
ARG OCI_SOURCE=https://github.com/shiweijiezero/foretoken
ARG OCI_REVISION
LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"

# The Omni image supplies Python, model plugins, and provider SDKs.
# Add shared preparation and the lifecycle adapter without applying standard vLLM patches.
FROM ${INFERENCE_ENGINE_IMAGE} AS omni-runtime

ARG OMNI_VIDEO_SYNC_TIMEOUT=4000
ENV VLLM_OMNI_VIDEO_SYNC_TIMEOUT=${OMNI_VIDEO_SYNC_TIMEOUT} \
    VLLM_OMNI_DLO_DP_WAVE_TIMEOUT=${OMNI_VIDEO_SYNC_TIMEOUT}

ARG OCI_SOURCE=https://github.com/shiweijiezero/foretoken
ARG OCI_REVISION
LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"
COPY --from=builder /out/bin/foretoken-model-server /usr/local/bin/foretoken-omni-model-server
COPY data-plane/model-server/python/foretoken_prepare.py /opt/foretoken/python/foretoken_prepare.py
COPY data-plane/model-server/python/foretoken_model_sources/ /opt/foretoken/python/foretoken_model_sources/
ENTRYPOINT ["foretoken-omni-model-server"]

# Native wheels and Python adaptation share the selected interpreter and ABI,
# but Python patch changes do not invalidate the native compilation branch.
FROM ${INFERENCE_ENGINE_IMAGE} AS inference-base
ARG FORETOKEN_VLLM_PYTHON
ENV FORETOKEN_VLLM_PYTHON=${FORETOKEN_VLLM_PYTHON}

FROM inference-base AS engine-base

# Patch the installed Python engine, not the independent Rust build source.
# A derived Foretoken image may already contain this exact backport.
RUN --mount=from=patch-tool,source=/usr/bin/patch,target=/usr/local/bin/patch \
    --mount=from=runtime-patches,source=/patches,target=/tmp/vllm-patches \
    "${FORETOKEN_VLLM_PYTHON:-python3}" /tmp/vllm-patches/apply.py

ARG UV_DEFAULT_INDEX
# Engine images may provide a Python virtual environment without pip.
# MACA also exposes torch.version.cuda, so select dependencies from the engine build.
# Precompile installed packages to avoid repeated bytecode compilation at startup.
RUN --mount=type=secret,id=foretoken-build-jobs,env=BUILD_JOBS \
    --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=from=runtime-patches,source=/patches,target=/tmp/vllm-patches \
    --mount=type=bind,source=data-plane/model-server/provider-requirements.txt,target=/tmp/provider-requirements.txt \
    python_path=$("${FORETOKEN_VLLM_PYTHON:-python3}" -c \
      'import sys; print(sys.executable)') \
    && PYTHONPATH="/tmp/vllm-patches${PYTHONPATH:+:$PYTHONPATH}" "$python_path" -c \
      'from vllm_patches import repair_installed_dependencies; repair_installed_dependencies()' \
    && backend=$("$python_path" -c \
      'import torch; print("maca" if getattr(torch.version, "maca", None) else "cuda" if torch.version.cuda else "other")') \
    && set -- -r /tmp/provider-requirements.txt \
    && case "$backend" in cuda) set -- "$@" bitsandbytes==0.49.2 modelexpress==0.6.0 nixl==1.4.1 ;; esac \
    && PYTHONPATH="/tmp/vllm-patches${PYTHONPATH:+:$PYTHONPATH}" "$python_path" -c \
      'from vllm_patches import runtime_constraints; print("\n".join(f"{name}==={version}" for name, version in sorted(runtime_constraints(set()).items())))' > /tmp/foretoken-engine-constraints.txt \
    && uv pip install --no-cache --python "$python_path" \
      --constraint /tmp/foretoken-engine-constraints.txt \
      --default-index "${UV_DEFAULT_INDEX}" "$@" \
    && PYTHONPATH="/tmp/vllm-patches${PYTHONPATH:+:$PYTHONPATH}" "$python_path" -c \
      'from vllm_patches import check_installed_dependencies; check_installed_dependencies()' \
    && "$python_path" -c 'import compileall, os, site; raise SystemExit(not all(compileall.compile_dir(path, quiet=1, workers=int(os.environ.get("BUILD_JOBS") or 1)) for path in site.getsitepackages()))' \
    && rm /tmp/foretoken-engine-constraints.txt

# Keep application changes and checkout permissions out of the native wheel's
# inputs, matching source snapshots without changing the caller's files.
FROM patch-tool AS mooncake-source
COPY deploy/mooncake/ /input/deploy/mooncake/
COPY data-plane/patches/vllm/mooncake.version /input/data-plane/patches/vllm/mooncake.version
# The required license also permits an absent optional third_party directory.
COPY LICENSE third_party* /input/third_party/
RUN find /input -type d -exec chmod 00755 {} + \
    && find /input -type f -perm -100 -exec chmod 0755 {} + \
    && find /input -type f ! -perm -100 -exec chmod 0644 {} +

FROM scratch AS mooncake-inputs
COPY --from=mooncake-source /input/ /

# Build against the selected Python/CUDA ABI. Runtime images consume the complete
# wheel and do not retain native compilers or build-time system packages.
FROM inference-base AS mooncake-build
USER root
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=mooncake-go /usr/local/go /usr/local/go
ARG UV_DEFAULT_INDEX
ARG GOSUMDB
# Session download settings leave native cache inputs unchanged; absent overrides
# preserve the base image's environment.
RUN --mount=type=secret,id=foretoken-build-jobs,env=BUILD_JOBS \
    --mount=type=secret,id=FORETOKEN_GITHUB_MIRROR \
    --mount=type=secret,id=GOPROXY \
    --mount=type=bind,from=mooncake-inputs,target=/input \
    --mount=type=cache,id=foretoken-mooncake-source,target=/var/cache/foretoken/mooncake \
    set -eu; \
    for name in FORETOKEN_GITHUB_MIRROR GOPROXY; do \
      if [ -f "/run/secrets/$name" ]; then \
        value=$(cat "/run/secrets/$name"); export "$name=$value"; \
      fi; \
    done; \
    "${FORETOKEN_VLLM_PYTHON:-python3}" -I /input/deploy/mooncake/build-cuda-wheel \
      --output /out --cache /var/cache/foretoken/mooncake --jobs "${BUILD_JOBS:-2}"

# The selected Torch ABI owns this small binding; source updates reuse it with the environment.
FROM mooncake-build AS profiling-build
RUN --mount=type=bind,source=data-plane/model-server/profiling,target=/input/profiling \
    "${FORETOKEN_VLLM_PYTHON:-python3}" -I /input/profiling/build.py --output /out/profiling

FROM engine-base AS engine-runtime
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=from=mooncake-build,source=/out,target=/tmp/mooncake-wheels \
    --mount=from=runtime-patches,source=/patches,target=/tmp/vllm-patches \
    PYTHONPATH="/tmp/vllm-patches${PYTHONPATH:+:$PYTHONPATH}" "${FORETOKEN_VLLM_PYTHON:-python3}" -c \
      'from pathlib import Path; from vllm_patches import install_mooncake_dependencies, check_installed_dependencies; install_mooncake_dependencies(Path("/tmp/mooncake-wheels")); check_installed_dependencies()'
RUN --mount=from=profiling-build,source=/out/profiling,target=/tmp/profiling \
    "${FORETOKEN_VLLM_PYTHON:-python3}" -c \
      'import shutil, sysconfig; shutil.copy2("/tmp/profiling/foretoken_profile_native.so", sysconfig.get_path("platlib"))'
COPY --from=mooncake-build /out/licenses/ /usr/local/share/licenses/
COPY --from=mooncake-inputs /deploy/mooncake/NOTICE /usr/local/share/foretoken-mooncake/NOTICE
COPY --from=mooncake-inputs /deploy/mooncake/patches/ /usr/local/share/foretoken-mooncake/patches/

FROM engine-runtime AS environment
ARG OCI_SOURCE=https://github.com/shiweijiezero/foretoken
ARG OCI_REVISION
LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"

FROM environment AS runtime
COPY --from=builder /out/bin/foretoken-model-server /usr/local/bin/foretoken-model-server
COPY data-plane/model-server/python/ /opt/foretoken/python/

# Preserve the inference engine image's runtime user and environment. The workload owns its
# security context, cache environment, and writable volume mounts.
ENTRYPOINT ["foretoken-model-server"]
