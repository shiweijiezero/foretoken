# syntax=docker/dockerfile:1.19
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build from the repository root so Cargo uses the data-plane workspace and
# its engine adapters. The inference engine image must provide the Python
# entrypoint required by the selected adapter. FORETOKEN_VLLM_PYTHON may select
# an image-specific executable; model-server otherwise uses `python3`. The
# current vLLM adapter uses vllm.entrypoints.cli.main.

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

FROM ${RUST_BUILDER_IMAGE} AS builder

ARG FORETOKEN_GITHUB_MIRROR
ARG FORETOKEN_CARGO_REGISTRY
ARG CARGO_NET_GIT_FETCH_WITH_CLI
ARG FORETOKEN_MODEL_SERVER_BINARY=foretoken-model-server
ARG VLLM_REVISION

WORKDIR /workspace

# Python adapters belong to the file export, not the Rust compilation input.
COPY --exclude=model-server/python data-plane/ ./data-plane/

# Lock only upstream preparation so other components can compile independently.
# Preserve captured Rust sources when completing the upstream tree for patch preparation.
RUN --mount=type=cache,id=foretoken-vllm-${VLLM_REVISION},target=/var/cache/vllm,sharing=locked \
    if [ -n "${FORETOKEN_GITHUB_MIRROR}" ]; then \
      printf '[url "%s/"]\n\tinsteadOf = https://github.com/\n' \
        "${FORETOKEN_GITHUB_MIRROR%/}" > /root/.gitconfig; \
    fi \
    && if [ -n "${FORETOKEN_CARGO_REGISTRY}" ] && [ "${FORETOKEN_CARGO_REGISTRY}" != "sparse+https://index.crates.io/" ]; then \
      mkdir -p /workspace/.cargo; \
      printf '[source.crates-io]\nreplace-with = "foretoken-mirror"\n\n[source.foretoken-mirror]\nregistry = "%s"\n' \
        "${FORETOKEN_CARGO_REGISTRY}" > /workspace/.cargo/config.toml; \
    fi \
    && if [ ! -f data-plane/third_party/vllm/vllm/v1/engine/core.py ]; then \
      if [ ! -f /var/cache/vllm/rust/Cargo.toml ]; then \
        git -C /var/cache/vllm init -q \
        && git -C /var/cache/vllm fetch --depth=1 https://github.com/vllm-project/vllm.git "${VLLM_REVISION}" \
        && git -C /var/cache/vllm checkout -q --detach FETCH_HEAD; \
      fi \
      && mkdir -p data-plane/third_party/vllm \
      && if [ -d data-plane/third_party/vllm/rust ]; then \
        git -C /var/cache/vllm archive HEAD | tar --anchored --exclude=rust -x -C data-plane/third_party/vllm; \
      else \
        git -C /var/cache/vllm archive HEAD | tar -x -C data-plane/third_party/vllm; \
      fi; \
    fi

# Share Cargo's own cache locks with its registry and Git data. Bullseye cannot
# load procedural macros linked against the frontend builder's newer glibc.
ENV CARGO_HOME=/var/cache/foretoken-cargo
# Retain prepared inputs with their outputs; refresh changed trees so Cargo sees patch removals.
RUN --mount=type=cache,id=foretoken-cargo-home,target=/var/cache/foretoken-cargo \
    --mount=type=cache,id=foretoken-cargo-target-model-server-prepared-source,target=/workspace/data-plane/target,sharing=locked \
    if [ -n "${CARGO_NET_GIT_FETCH_WITH_CLI}" ]; then \
      export CARGO_NET_GIT_FETCH_WITH_CLI; \
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
      --package foretoken-model-server --bin "${FORETOKEN_MODEL_SERVER_BINARY}" \
    && install -Dm755 \
      "data-plane/target/release/${FORETOKEN_MODEL_SERVER_BINARY}" \
      /out/model-server

FROM scratch AS source-export
COPY --from=builder /out/model-server /bin/foretoken-model-server
COPY data-plane/model-server/python/ /python/
COPY LICENSE /LICENSE
COPY <<EOF /complete.json
{"component":"model-server"}
EOF

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
COPY --from=builder /out/model-server /usr/local/bin/foretoken-model-server
COPY data-plane/model-server/python/ /opt/foretoken/python/

# Preserve the inference engine image's runtime user and environment. The workload owns its
# security context, cache environment, and writable volume mounts.
ENTRYPOINT ["foretoken-model-server"]
