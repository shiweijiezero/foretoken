# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

ARG BASE_IMAGE_REGISTRY=docker.io
ARG UV_IMAGE_REGISTRY=ghcr.io
ARG UV_IMAGE=${UV_IMAGE_REGISTRY}/astral-sh/uv:0.9.10
FROM ${UV_IMAGE} AS uv

FROM ${BASE_IMAGE_REGISTRY}/library/python:3.10-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential ca-certificates libnuma1 libibverbs1 ibverbs-providers \
      librdmacm1 libcurl4 libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

ARG UV_DEFAULT_INDEX=https://pypi.org/simple
COPY deploy/inference-engines/vllm-cuda/requirements.txt /tmp/engine-requirements.txt
# Native upstream wheels own CUDA/PyTorch dependencies; no Foretoken engine fork.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,id=foretoken-vllm-cuda-uv,target=/root/.cache/uv \
    uv pip install --system --torch-backend=cu130 \
      --default-index "${UV_DEFAULT_INDEX}" \
      --extra-index-url https://wheels.vllm.ai/3b4566c5cf014605de6aeab6eb831b4f20511c17 \
      -r /tmp/engine-requirements.txt \
    && uv pip check --system \
    && rm /tmp/engine-requirements.txt

# FlashInfer JIT uses the compiler and headers supplied by the CUDA wheels.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    CUDA_HOME=/usr/local/lib/python3.10/site-packages/nvidia/cu13

ENV PATH=${CUDA_HOME}/bin:${PATH}

# CUDA wheels use lib/ and versioned runtime names; FlashInfer expects Toolkit layout.
RUN ln -s lib "${CUDA_HOME}/lib64" \
    && ln -s libcudart.so.13 "${CUDA_HOME}/lib/libcudart.so"

WORKDIR /workspace
