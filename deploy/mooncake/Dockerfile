# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

ARG BASE_IMAGE_REGISTRY=docker.io
ARG BUILD_IMAGE=${BASE_IMAGE_REGISTRY}/library/ubuntu:24.04
ARG RUNTIME_IMAGE=${BASE_IMAGE_REGISTRY}/library/ubuntu:24.04
ARG GO_IMAGE=${BASE_IMAGE_REGISTRY}/library/golang:1.25.10-bookworm

FROM ${GO_IMAGE} AS go

FROM ${BUILD_IMAGE} AS build

ARG BUILD_JOBS=2
ARG GOPROXY
ARG GOSUMDB
COPY --from=go /usr/local/go /usr/local/go
ENV PATH=/usr/local/go/bin:${PATH}
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates cmake ninja-build pkg-config python3-dev \
    libasio-dev libboost-dev libcurl4-openssl-dev libgflags-dev \
    libgoogle-glog-dev libibverbs-dev libjsoncpp-dev libmsgpack-dev libnuma-dev \
    libssl-dev liburing-dev libxxhash-dev libyaml-cpp-dev libzstd-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/mooncake
COPY third_party/mooncake/ ./

# Older supported releases vendor the RPC dependency as a submodule. Newer
# releases fetch their pinned source during the main CMake configuration.
RUN if [ -d extern/yalantinglibs ]; then \
      cmake -S extern/yalantinglibs -B /build/ylt \
        -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARK=OFF -DBUILD_UNIT_TESTS=OFF; \
      cmake --install /build/ylt; \
    fi

# The Store image does not own GPU requester runtimes or Python wheel lifecycles.
RUN cmake -S . -B /build/mooncake -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
      -DCMAKE_INSTALL_RPATH=/usr/local/lib/mooncake \
      -DUSE_CUDA=OFF -DUSE_MACA=OFF -DUSE_TCP=ON \
      -DUSE_ETCD=OFF -DSTORE_USE_ETCD=ON -DUSE_HTTP=ON \
      -DWITH_STORE_RUST=OFF -DWITH_EP=OFF \
      -DBUILD_UNIT_TESTS=OFF -DBUILD_BENCHMARK=OFF \
      -DPython3_EXECUTABLE=/usr/bin/python3 \
    && cmake --build /build/mooncake \
      --target mooncake_master mooncake_client --parallel "${BUILD_JOBS}" \
    && install -Dm755 /build/mooncake/mooncake-store/src/mooncake_master /out/bin/mooncake_master \
    && install -Dm755 /build/mooncake/mooncake-store/src/mooncake_client /out/bin/mooncake_client \
    && install -Dm755 /build/mooncake/mooncake-common/libasio.so /out/lib/mooncake/libasio.so \
    && install -Dm755 /build/mooncake/mooncake-common/etcd/libetcd_wrapper.so /out/lib/mooncake/libetcd_wrapper.so \
    && install -Dm644 LICENSE-APACHE /out/share/licenses/mooncake/LICENSE-APACHE

RUN ylt_source=extern/yalantinglibs \
    && if [ ! -d "${ylt_source}" ]; then ylt_source=/build/mooncake/_deps/yalantinglibs-src; fi \
    && install -Dm644 "${ylt_source}/LICENSE" /out/share/licenses/yalantinglibs/LICENSE \
    && install -Dm644 "${ylt_source}/NOTICE" /out/share/licenses/yalantinglibs/NOTICE \
    && install -Dm644 /usr/share/doc/libasio-dev/copyright /out/share/licenses/asio/copyright \
    && install -Dm644 /usr/share/doc/libboost1.83-dev/copyright /out/share/licenses/boost/copyright

FROM ${RUNTIME_IMAGE}

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ibverbs-providers libcurl4t64 libgflags2.2 libgoogle-glog0v6t64 \
    libibverbs1 libjsoncpp25 libnuma1 libstdc++6 liburing2 \
    libxxhash0 libyaml-cpp0.8 libzstd1 tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 mooncake \
    && useradd --uid 65532 --gid 65532 --create-home --home-dir /home/mooncake mooncake

COPY --from=build /out/ /usr/local/

# Kubernetes command overrides must retain init-based signal forwarding and reaping.
RUN mkdir -p /usr/local/libexec \
    && for binary in mooncake_master mooncake_client; do \
        mv "/usr/local/bin/$binary" "/usr/local/libexec/$binary"; \
        printf '#!/bin/sh\nexec /usr/bin/tini -- /usr/local/libexec/%s "$@"\n' "$binary" \
            > "/usr/local/bin/$binary"; \
        chmod 755 "/usr/local/bin/$binary"; \
    done

COPY LICENSE /usr/local/share/licenses/foretoken/LICENSE
COPY deploy/mooncake/NOTICE /usr/local/share/foretoken-mooncake/NOTICE
COPY deploy/mooncake/patches/ /usr/local/share/foretoken-mooncake/patches/
WORKDIR /home/mooncake
USER 65532:65532
CMD ["mooncake_master"]
