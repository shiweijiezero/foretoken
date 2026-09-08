# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.10
ARG METAX_SDK_IMAGE
FROM ${UV_IMAGE} AS uv
FROM ${METAX_SDK_IMAGE}

ARG VLLM_VERSION
ARG MACA_PATH=/opt/maca

# The SDK image supplies MACA tools and shared libraries; the installer resolves Python packages.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      bash build-essential ca-certificates curl patch python3-dev \
      libelf1 libnuma1 libglib2.0-0 libpng-dev libjpeg-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
COPY install.sh /usr/local/lib/foretoken/install-vllm-metax.sh
COPY xgrammar-0.24.patch constraints-0.24.txt /usr/local/lib/foretoken/

ENV MACA_PATH=${MACA_PATH} \
    UV_PYTHON=/usr/bin/python3
RUN bash /usr/local/lib/foretoken/install-vllm-metax.sh \
    /opt/foretoken-vllm ${VLLM_VERSION}

ENV VIRTUAL_ENV=/opt/foretoken-vllm/.venv \
    PATH=/opt/foretoken-vllm/.venv/bin:${PATH} \
    LD_LIBRARY_PATH=${MACA_PATH}/lib:${MACA_PATH}/ompi/lib:${MACA_PATH}/mxgpu_llvm/lib:${LD_LIBRARY_PATH}

WORKDIR /workspace
