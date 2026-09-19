# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

ARG UV_IMAGE_REGISTRY=ghcr.io
ARG UV_IMAGE=${UV_IMAGE_REGISTRY}/astral-sh/uv:0.9.10
ARG METAX_SDK_IMAGE
FROM ${UV_IMAGE} AS uv

FROM ${METAX_SDK_IMAGE} AS mooncake-build

ARG BUILD_JOBS=2
ARG MACA_PATH=/opt/maca

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential ca-certificates cmake ninja-build pkg-config python3-dev \
      libasio-dev libboost-dev libcurl4-openssl-dev libgflags-dev \
      libgoogle-glog-dev libibverbs-dev libjsoncpp-dev libmsgpack-dev libnuma-dev \
      libssl-dev liburing-dev libxxhash-dev libyaml-cpp-dev libzstd-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
COPY third_party/mooncake/ /src/mooncake/

ENV MACA_PATH=${MACA_PATH} \
    MACA_HOME=${MACA_PATH} \
    LD_LIBRARY_PATH=${MACA_PATH}/lib64:${MACA_PATH}/lib:${MACA_PATH}/ompi/lib:${MACA_PATH}/mxgpu_llvm/lib:${LD_LIBRARY_PATH}

# vLLM consumes only the TransferEngine and Store requester Python modules.
RUN cmake -S /src/mooncake/extern/yalantinglibs -B /build/yalantinglibs \
      -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARK=OFF -DBUILD_UNIT_TESTS=OFF \
    && cmake --install /build/yalantinglibs \
    && uv venv /opt/mooncake-build --python /usr/bin/python3 \
    && cmake -S /src/mooncake -B /build/mooncake -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DUSE_MACA=ON -DMACA_ROOT="${MACA_PATH}" \
      -DUSE_TCP=ON -DUSE_ETCD=OFF -DUSE_HTTP=ON \
      -DWITH_TE=ON -DWITH_STORE=ON -DWITH_STORE_RUST=OFF -DWITH_EP=OFF \
      -DBUILD_UNIT_TESTS=OFF -DBUILD_BENCHMARK=OFF -DBUILD_EXAMPLES=OFF \
      -DPython3_EXECUTABLE=/opt/mooncake-build/bin/python \
    && cmake --build /build/mooncake --target engine store --parallel "${BUILD_JOBS}" \
    && install -d /out/mooncake \
    && install -m 644 /build/mooncake/mooncake-integration/mooncake/__init__.py \
      /build/mooncake/mooncake-common/libasio.so \
      /build/mooncake/mooncake-integration/engine.*.so \
      /build/mooncake/mooncake-integration/store.*.so \
      /out/mooncake/
RUN install -Dm644 /src/mooncake/LICENSE-APACHE \
      /out/licenses/mooncake/LICENSE-APACHE \
    && install -Dm644 /src/mooncake/extern/pybind11/LICENSE \
      /out/licenses/pybind11/LICENSE \
    && install -Dm644 /src/mooncake/extern/yalantinglibs/LICENSE \
      /out/licenses/yalantinglibs/LICENSE \
    && install -Dm644 /src/mooncake/extern/yalantinglibs/NOTICE \
      /out/licenses/yalantinglibs/NOTICE \
    && install -Dm644 /usr/share/doc/libasio-dev/copyright /out/licenses/asio/copyright \
    && boost_package=$(dpkg-query -S /usr/include/boost/version.hpp) \
    && install -Dm644 "/usr/share/doc/${boost_package%%:*}/copyright" /out/licenses/boost/copyright

FROM ${METAX_SDK_IMAGE}

ARG VLLM_VERSION
ARG MACA_PATH=/opt/maca
ARG UV_PYTHON
ARG FORETOKEN_GITHUB_MIRROR
ARG UV_DEFAULT_INDEX
ARG UV_EXTRA_INDEX_URL

# The SDK image supplies MACA tools and shared libraries; the installer resolves vLLM packages.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      bash build-essential ca-certificates curl patch python3-dev \
      libelf1 libnuma1 libglib2.0-0 libpng-dev libjpeg-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
COPY deploy/inference-engines/vllm-metax/install.sh /usr/local/lib/foretoken/install-vllm-metax.sh
COPY deploy/inference-engines/vllm-metax/xgrammar-0.24.patch \
     deploy/inference-engines/vllm-metax/constraints-0.24.txt \
     deploy/inference-engines/vllm-metax/dependencies-0.26.patch \
     deploy/inference-engines/vllm-metax/sdk_audio.py \
     /usr/local/lib/foretoken/

ENV MACA_PATH=${MACA_PATH} \
    UV_PYTHON=${UV_PYTHON}
RUN bash /usr/local/lib/foretoken/install-vllm-metax.sh \
    /opt/foretoken-vllm ${VLLM_VERSION}

COPY --from=mooncake-build /out/mooncake/ /opt/foretoken-mooncake/mooncake/
COPY --from=mooncake-build /out/licenses/ /usr/local/share/licenses/
RUN mooncake_site=$(/opt/foretoken-vllm/.venv/bin/python -c \
      'import sysconfig; print(sysconfig.get_path("platlib"))') \
    && cp -a /opt/foretoken-mooncake/mooncake "${mooncake_site}/" \
    && uv pip check --python /opt/foretoken-vllm/.venv/bin/python \
    && rm -rf /opt/foretoken-mooncake

# Development package names resolve the matching runtime ABI across Debian-based SDK images.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ibverbs-providers libcurl4-openssl-dev libgflags2.2 libgoogle-glog-dev \
      libibverbs1 libjsoncpp-dev libnuma1 libstdc++6 liburing2 libxxhash0 \
      libyaml-cpp-dev libzstd1 \
    && rm -rf /var/lib/apt/lists/*

# Let Mooncake use verbs for inter-node transfers instead of MACA accelerator transport.
ENV MC_MACA_HOST_TRANSPORT=1 \
    VIRTUAL_ENV=/opt/foretoken-vllm/.venv \
    PATH=/opt/foretoken-vllm/.venv/bin:${PATH} \
    LD_LIBRARY_PATH=${MACA_PATH}/lib64:${MACA_PATH}/lib:${MACA_PATH}/ompi/lib:${MACA_PATH}/mxgpu_llvm/lib:${LD_LIBRARY_PATH}

COPY deploy/mooncake/NOTICE /usr/local/share/foretoken-mooncake/NOTICE
COPY deploy/mooncake/patches/ /usr/local/share/foretoken-mooncake/patches/

WORKDIR /workspace
