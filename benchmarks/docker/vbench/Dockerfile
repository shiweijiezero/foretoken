# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ARG VBENCH_REVISION=fd18b3d055cb0fc6f066ca90fe2c3c8cbb698490
LABEL org.opencontainers.image.source="https://github.com/shiweijiezero/foretoken" \
      org.foretoken.vbench.commit="${VBENCH_REVISION}"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential ca-certificates ffmpeg git libgl1 libglib2.0-0 \
      python3 python3-dev python3-pip python3-venv unzip wget \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/vbench-env
ENV PATH="/opt/vbench-env/bin:${PATH}"

RUN python3 -m pip install --no-cache-dir \
      torch==2.5.1+cu121 torchvision==0.20.1+cu121 \
      --index-url https://download.pytorch.org/whl/cu121

# Drop unused upstream archives and Git history before committing the runtime layer.
RUN git clone https://github.com/Vchitect/VBench.git /opt/VBench \
    && git -C /opt/VBench checkout --detach "${VBENCH_REVISION}" \
    && rm -r /opt/VBench/VBench-2.0/vbench2/third_party/LLaVA_NeXT/scripts/archived /opt/VBench/.git \
    && python3 -m pip install --no-cache-dir \
      -r /opt/VBench/requirements.txt \
      torch==2.5.1+cu121 torchvision==0.20.1+cu121 \
      pyiqa==0.1.10 'setuptools<81' \
      --extra-index-url https://download.pytorch.org/whl/cu121 \
    && python3 -m pip check

WORKDIR /opt/VBench
