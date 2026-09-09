# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

ARG METAX_BASE_IMAGE
FROM ${METAX_BASE_IMAGE} AS builder

ARG MACA_PATH=/opt/maca
ARG METAX_PYTHON=/opt/conda/bin/python
ARG UV_VERSION=0.9.10
ARG VLLM_VERSION=0.24.0
ARG PYPI_INDEX_URL=https://pypi.org/simple
ARG METAX_INDEX_URL=https://repos.metax-tech.com/r/maca-pypi/simple

ENV MACA_PATH=${MACA_PATH}
ENV CUCC_PATH=${MACA_PATH}/tools/cu-bridge \
    CUDA_PATH=/root/cu-bridge/CUDA_DIR \
    CUCC_CMAKE_ENTRY=2
ENV PATH=${MACA_PATH}/mxgpu_llvm/bin:${MACA_PATH}/bin:${CUCC_PATH}/tools:${CUCC_PATH}/bin:${PATH} \
    LD_LIBRARY_PATH=${MACA_PATH}/lib:${MACA_PATH}/ompi/lib:${MACA_PATH}/mxgpu_llvm/lib:${LD_LIBRARY_PATH}
ENV UV_INDEX_URL=${PYPI_INDEX_URL} \
    UV_EXTRA_INDEX_URL=${METAX_INDEX_URL} \
    UV_INDEX_STRATEGY=unsafe-best-match \
    VIRTUAL_ENV=/opt/foretoken-vllm \
    FORETOKEN_VLLM_PYTHON=/opt/foretoken-vllm/bin/python

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && ${METAX_PYTHON} -m pip install --no-cache-dir \
      --index-url ${PYPI_INDEX_URL} \
      uv==${UV_VERSION} \
    && ln -sf \
      "$(${METAX_PYTHON} -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/uv" \
      /usr/local/bin/uv \
    && uv venv ${VIRTUAL_ENV} \
      --python ${METAX_PYTHON} \
      --system-site-packages

WORKDIR /opt/src

RUN mkdir vLLM-metax vllm \
    && curl --fail --location --output /tmp/vllm-metax.tar.gz \
      https://codeload.github.com/MetaX-MACA/vLLM-metax/tar.gz/refs/tags/v${VLLM_VERSION} \
    && tar --extract --gzip --strip-components=1 \
      --file /tmp/vllm-metax.tar.gz \
      --directory vLLM-metax \
    && curl --fail --location --output /tmp/vllm.tar.gz \
      https://codeload.github.com/vllm-project/vllm/tar.gz/refs/tags/v${VLLM_VERSION} \
    && tar --extract --gzip --strip-components=1 \
      --file /tmp/vllm.tar.gz \
      --directory vllm

WORKDIR /opt/src/vLLM-metax

RUN uv pip install --python ${FORETOKEN_VLLM_PYTHON} \
      -r requirements/build.txt \
    && SETUPTOOLS_SCM_PRETEND_VERSION=${VLLM_VERSION} \
      uv pip install --python ${FORETOKEN_VLLM_PYTHON} \
      --no-build-isolation \
      --no-deps \
      .

WORKDIR /opt/src/vllm

RUN VLLM_TARGET_DEVICE=empty \
    VLLM_VERSION_OVERRIDE=${VLLM_VERSION} \
    uv pip install --python ${FORETOKEN_VLLM_PYTHON} \
      --no-build-isolation \
      --no-deps \
      . \
    && ${FORETOKEN_VLLM_PYTHON} -c \
      'import vllm, vllm_metax; print(vllm.__version__)'

FROM ${METAX_BASE_IMAGE}

ENV VIRTUAL_ENV=/opt/foretoken-vllm \
    PATH=/opt/foretoken-vllm/bin:${PATH} \
    FORETOKEN_VLLM_PYTHON=/opt/foretoken-vllm/bin/python

COPY --from=builder /opt/foretoken-vllm /opt/foretoken-vllm

WORKDIR /workspace
