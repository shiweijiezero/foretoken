# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

ARG RUNTIME_IMAGE
ARG UV_IMAGE_REGISTRY=ghcr.io
ARG UV_IMAGE=${UV_IMAGE_REGISTRY}/astral-sh/uv:0.9.10
FROM ${UV_IMAGE} AS uv

FROM ${RUNTIME_IMAGE} AS runtime-user
RUN mkdir -p /tmp/foretoken-runtime-user \
    && printf '%s:%s\n' "$(id -u)" "$(id -g)" > /tmp/foretoken-runtime-user/user

FROM scratch AS runtime-user-export
COPY --from=runtime-user /tmp/foretoken-runtime-user/user /user

# Add build tools to the exact serving environment, leaving torch and vendor libraries intact.
FROM ${RUNTIME_IMAGE} AS toolchain
ARG UV_DEFAULT_INDEX
ARG UV_EXTRA_INDEX_URL
ARG FORETOKEN_GITHUB_MIRROR
USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential ccache cmake git ninja-build patch \
    && cuda=$("${FORETOKEN_VLLM_PYTHON:-python}" -c 'import torch; print("" if getattr(torch.version, "maca", None) else torch.version.cuda or "")') \
    && if [ -n "$cuda" ] && ! command -v nvcc >/dev/null 2>&1 && ! test -x "${CUDA_HOME:-/usr/local/cuda}/bin/nvcc"; then \
      version=$(printf %s "$cuda" | tr . -); \
      set -- "cuda-nvcc-$version"; \
      for library in cuda-cudart cuda-nvrtc libcublas libcurand libcusparse libcusolver; do \
        installed=$(dpkg-query -W -f='${Version}' "$library-$version" 2>/dev/null || true); \
        if [ -n "$installed" ]; then \
          set -- "$@" "$library-dev-$version=$installed"; \
        else \
          set -- "$@" "$library-dev-$version"; \
        fi; \
      done; \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"; \
    fi \
    && if [ -n "$FORETOKEN_GITHUB_MIRROR" ]; then \
      git config --global "url.${FORETOKEN_GITHUB_MIRROR%/}/.insteadOf" https://github.com/; \
    fi \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
RUN python="${FORETOKEN_VLLM_PYTHON:-python}" \
    && uv pip install --python "$python" --target /opt/foretoken-engine-build/python \
      'cmake>=3.26.1' ninja 'packaging>=24.2' 'setuptools>=77.0.3,<81' \
      'setuptools-scm>=8' 'setuptools-rust>=1.9' wheel jinja2 build
ENV PATH=/opt/foretoken-engine-build/python/bin:${PATH}
COPY deploy/inference-engines/source-build.py /opt/foretoken-engine-build/source-build.py
COPY data-plane/patches/vllm/ /opt/foretoken-engine-build/patches/
COPY data-plane/patches/vllm/vllm_patches.py /opt/foretoken-engine-build/
COPY deploy/inference-engines/vllm-metax/source-environment.json \
     deploy/inference-engines/vllm-metax/sdk_audio.py \
     /opt/foretoken-engine-build/metax/
COPY deploy/inference-engines/vllm-metax/patches/ /opt/foretoken-engine-build/metax/patches/

FROM toolchain AS engine-build
ARG CACHE_ID
ARG BUILD_NATIVE=false
ARG BUILD_JOBS
ARG TARGET_CUDA_ARCH_LIST
COPY engine/ /input/engine/
# The cache identity belongs to the installation binding and runtime environment.
# All compiler state and successful native outputs survive Python-only updates.
RUN --mount=type=cache,id=${CACHE_ID},target=/cache,sharing=locked \
    bash -euc ' \
      if test -f /opt/foretoken-vllm/activate; then source /opt/foretoken-vllm/activate; fi; \
      export PYTHONPATH=/opt/foretoken-engine-build/python${PYTHONPATH:+:$PYTHONPATH}; \
      if test -n "$2"; then export MAX_JOBS="$2"; fi; \
      if test -n "$3"; then export TORCH_CUDA_ARCH_LIST="$3"; fi; \
      arguments=(--source-root /input/engine --cache /cache --output /out); \
      if test "$1" = true; then arguments+=(--build-native); fi; \
      "${FORETOKEN_VLLM_PYTHON:-python}" /opt/foretoken-engine-build/source-build.py "${arguments[@]}" \
    ' source-build "${BUILD_NATIVE}" "${BUILD_JOBS}" "${TARGET_CUDA_ARCH_LIST}"

FROM scratch AS source-export
COPY --from=engine-build /out/engine/ /engine/
COPY --from=engine-build /out/engine-environment.json /engine-environment.json

# Prepare new distribution metadata around the already-built native payload.
FROM engine-build AS engine-wheel-build
RUN --mount=type=cache,id=${CACHE_ID},target=/cache,sharing=locked \
    bash -euc ' \
      if test -f /opt/foretoken-vllm/activate; then source /opt/foretoken-vllm/activate; fi; \
      export PYTHONPATH=/opt/foretoken-engine-build/python${PYTHONPATH:+:$PYTHONPATH}; \
      "${FORETOKEN_VLLM_PYTHON:-python}" /opt/foretoken-engine-build/source-build.py \
        --source-root /input/engine --cache /cache --output /out --package-wheels \
    '

FROM ${RUNTIME_IMAGE} AS runtime
ARG UV_DEFAULT_INDEX
ARG UV_EXTRA_INDEX_URL
ARG RUNTIME_USER
USER root
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=from=engine-wheel-build,source=/out/wheels,target=/tmp/foretoken-engine-wheels \
    --mount=from=engine-wheel-build,source=/out/runtime-native-constraints.txt,target=/tmp/foretoken-native-constraints.txt \
    bash -euc ' \
      if test -f /opt/foretoken-vllm/activate; then source /opt/foretoken-vllm/activate; fi; \
      python="${FORETOKEN_VLLM_PYTHON:-python}"; \
      if "$python" -c "import torch; raise SystemExit(not bool(getattr(torch.version, \"maca\", None)))"; then \
        export UV_EXTRA_INDEX_URL=${UV_EXTRA_INDEX_URL:-https://repos.metax-tech.com/r/maca-pypi/simple}; \
        export UV_INDEX_STRATEGY=${UV_INDEX_STRATEGY:-unsafe-best-match}; \
      fi; \
      uv pip install --no-cache --python "$python" \
        --constraint /tmp/foretoken-native-constraints.txt \
        --reinstall-package vllm --reinstall-package vllm-metax \
        /tmp/foretoken-engine-wheels/*.whl; \
      uv pip check --python "$python" \
    '
# Packages are installed by uv; only activation settings remain outside site-packages.
COPY --from=engine-build /out/engine-environment.json /opt/foretoken/engine-source/engine-environment.json
ENV FORETOKEN_ENGINE_DIRECTORY=/opt/foretoken/engine-source
USER ${RUNTIME_USER}
