# syntax=docker/dockerfile:1.19
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Builds the control-plane manager and CRD bootstrapper container image.

ARG GO_IMAGE_REGISTRY=docker.io
ARG DISTROLESS_IMAGE_REGISTRY=gcr.io
ARG UV_IMAGE_REGISTRY=ghcr.io
ARG UV_IMAGE=${UV_IMAGE_REGISTRY}/astral-sh/uv:0.12.22
ARG GO_BUILDER_IMAGE=${GO_IMAGE_REGISTRY}/library/golang:1.26.5-bookworm@sha256:1ecb7edf62a0408027bd5729dfd6b1b8766e578e8df93995b225dfd0944eb651
ARG RUNTIME_IMAGE=${DISTROLESS_IMAGE_REGISTRY}/distroless/static-debian13:nonroot@sha256:f7f8f729987ad0fdf6b05eeeae94b26e6a0f613bdf46feea7fc40f7bd72953e6

FROM ${UV_IMAGE} AS uv

FROM ${GO_BUILDER_IMAGE} AS compiler
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/foretoken-python
ARG FORETOKEN_GITHUB_MIRROR
RUN mirror="${FORETOKEN_GITHUB_MIRROR:-https://github.com}"; \
    UV_PYTHON_INSTALL_MIRROR="${mirror%/}/astral-sh/python-build-standalone/releases/download" \
      uv venv /opt/foretoken-compiler --python 3.12
ENV PATH=/opt/foretoken-compiler/bin:${PATH}
WORKDIR /workspace

FROM compiler AS builder
ARG GOPROXY
ARG GOSUMDB
COPY control-plane/ ./control-plane/
COPY deploy/build-source ./deploy/build-source
COPY LICENSE ./LICENSE
RUN --mount=type=secret,id=foretoken-build-jobs,env=BUILD_JOBS \
    --mount=type=cache,id=foretoken-go-modules,target=/var/cache/foretoken/go-modules \
    --mount=type=cache,id=foretoken-go-build,target=/var/cache/foretoken/go-build \
    python3 -I deploy/build-source control-plane /out --cache /var/cache/foretoken

FROM scratch AS source-export
COPY --from=builder /out/ /

FROM ${RUNTIME_IMAGE} AS environment
ARG OCI_SOURCE=https://github.com/shiweijiezero/foretoken
ARG OCI_REVISION
LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"
USER 65532:65532

FROM environment AS runtime
COPY --from=builder /out/bin/manager /manager
COPY --from=builder /out/bin/crd-apply /crd-apply
COPY --from=builder /out/bin/video-worker /video-worker
COPY --from=builder /out/crds/ /opt/foretoken/crds/
COPY --from=builder /out/LICENSE /LICENSE
USER 65532:65532
ENTRYPOINT ["/manager"]
