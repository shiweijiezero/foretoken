# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Add Nsight Systems to a source-built NVIDIA model-server image (Linux x86_64).
ARG MODEL_SERVER_IMAGE
FROM debian:bookworm-slim AS nsight
ADD --checksum=sha256:c7cfe27e2250eb91e1a67e7feb5f2c490c7f598e3b3a3d047aff000bc49f9d6b \
    https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/nsight-systems-2025.3.2_2025.3.2.474-1_amd64.deb /tmp/nsight.deb
RUN dpkg-deb --extract /tmp/nsight.deb /nsight

FROM ${MODEL_SERVER_IMAGE}
# Preserve NVIDIA's tools, bundled dependencies and license files in the diagnostic image.
COPY --from=nsight /nsight/opt/nvidia/nsight-systems/2025.3.2/ /opt/nvidia/nsight-systems/2025.3.2/
ENV PATH="/opt/nvidia/nsight-systems/2025.3.2/target-linux-x64:${PATH}"
RUN nsys --version
