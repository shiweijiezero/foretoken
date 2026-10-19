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

FROM ${UV_IMAGE} AS uv

# Keep the build-time patch utility out of the inference image.
FROM ${BASE_IMAGE_REGISTRY}/library/debian:bookworm-slim AS patch-tool
RUN apt-get update \
    && apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/*

FROM ${RUST_BUILDER_IMAGE} AS compiler
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/foretoken-python
ARG FORETOKEN_GITHUB_MIRROR
RUN mirror="${FORETOKEN_GITHUB_MIRROR:-https://github.com}"; \
    UV_PYTHON_INSTALL_MIRROR="${mirror%/}/astral-sh/python-build-standalone/releases/download" \
      uv venv /opt/foretoken-compiler --python 3.12
ENV PATH=/opt/foretoken-compiler/bin:${PATH}
WORKDIR /workspace

FROM compiler AS builder
ARG FORETOKEN_GITHUB_MIRROR
ARG FORETOKEN_CARGO_REGISTRY
ARG CARGO_NET_GIT_FETCH_WITH_CLI
ARG FORETOKEN_MODEL_SERVER_BINARY=foretoken-model-server
ARG BUILD_JOBS
COPY --exclude=model-server/python data-plane/ ./data-plane/
COPY deploy/build-source ./deploy/build-source
COPY LICENSE ./LICENSE
ENV CARGO_HOME=/var/cache/foretoken/cargo-home
# Keep this target separate from the frontend's newer glibc toolchain.
RUN --mount=type=cache,id=foretoken-vllm-source,target=/var/cache/foretoken/vllm \
    --mount=type=cache,id=foretoken-cargo-home,target=/var/cache/foretoken/cargo-home \
    --mount=type=cache,id=foretoken-cargo-target-model-server-prepared-source,target=/workspace/data-plane/target,sharing=locked \
    python3 -I deploy/build-source model-server /out --cache /var/cache/foretoken --parts rust

FROM scratch AS source-export
COPY --from=builder /out/ /
COPY data-plane/model-server/python/ /python/

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
ENTRYPOINT ["foretoken-omni-model-server"]

FROM ${INFERENCE_ENGINE_IMAGE} AS engine-runtime

ARG FORETOKEN_VLLM_PYTHON
ENV FORETOKEN_VLLM_PYTHON=${FORETOKEN_VLLM_PYTHON}

# Patch the installed Python engine, not the independent Rust build source.
# A derived Foretoken image may already contain this exact backport.
RUN --mount=from=patch-tool,source=/usr/bin/patch,target=/usr/local/bin/patch \
    --mount=type=bind,source=data-plane/patches/vllm,target=/tmp/vllm-patches \
    "${FORETOKEN_VLLM_PYTHON:-python3}" /tmp/vllm-patches/apply.py

ARG UV_DEFAULT_INDEX=https://pypi.org/simple
# Engine images may provide a Python virtual environment without pip.
# MACA also exposes torch.version.cuda, so select dependencies from the engine build.
# Precompile installed packages to avoid repeated bytecode compilation at startup.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=data-plane/patches/vllm,target=/tmp/vllm-patches \
    python_path=$("${FORETOKEN_VLLM_PYTHON:-python3}" -c \
      'import sys; print(sys.executable)') \
    && PYTHONPATH="/tmp/vllm-patches${PYTHONPATH:+:$PYTHONPATH}" "$python_path" -c \
      'from vllm_patches import repair_installed_dependencies; repair_installed_dependencies()' \
    && backend=$("$python_path" -c \
      'import torch; print("maca" if getattr(torch.version, "maca", None) else "cuda" if torch.version.cuda else "other")') \
    && set -- modelscope==1.35.3 tzdata \
    && case "$backend" in cuda) set -- "$@" bitsandbytes==0.49.2 modelexpress==0.6.0 nixl==1.4.1 ;; esac \
    && "$python_path" -c 'from importlib.metadata import version; print("torch==" + version("torch")); print("vllm==" + version("vllm"))' > /tmp/foretoken-engine-constraints.txt \
    && uv pip install --no-cache --python "$python_path" \
      --constraint /tmp/foretoken-engine-constraints.txt \
      --default-index "${UV_DEFAULT_INDEX}" "$@" \
    && PYTHONPATH="/tmp/vllm-patches${PYTHONPATH:+:$PYTHONPATH}" "$python_path" -c \
      'from vllm_patches import install_mooncake_dependencies, check_installed_dependencies; install_mooncake_dependencies(); check_installed_dependencies()' \
    && "$python_path" -c 'import compileall, site; raise SystemExit(not all(compileall.compile_dir(path, quiet=1) for path in site.getsitepackages()))' \
    && rm /tmp/foretoken-engine-constraints.txt

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
