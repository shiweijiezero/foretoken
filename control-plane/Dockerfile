# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Builds the control-plane manager and CRD bootstrapper container image.

ARG GO_IMAGE_REGISTRY=docker.io
ARG DISTROLESS_IMAGE_REGISTRY=gcr.io
ARG GO_BUILDER_IMAGE=${GO_IMAGE_REGISTRY}/library/golang:1.26.5-bookworm@sha256:1ecb7edf62a0408027bd5729dfd6b1b8766e578e8df93995b225dfd0944eb651
ARG RUNTIME_IMAGE=${DISTROLESS_IMAGE_REGISTRY}/distroless/static-debian13:nonroot@sha256:f7f8f729987ad0fdf6b05eeeae94b26e6a0f613bdf46feea7fc40f7bd72953e6

FROM ${GO_BUILDER_IMAGE} AS builder
ARG GOPROXY
ARG GOSUMDB
WORKDIR /workspace/control-plane
COPY control-plane/go.mod control-plane/go.sum ./
RUN if [ -n "${GOPROXY}" ]; then go env -w GOPROXY="${GOPROXY}"; fi \
    && if [ -n "${GOSUMDB}" ]; then go env -w GOSUMDB="${GOSUMDB}"; fi \
    && go mod download
COPY control-plane/ ./
# Build the long-running manager and the one-shot CRD bootstrapper from the same source tree.
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/manager ./cmd && \
    CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/crd-apply ./cmd/crd-apply

FROM ${RUNTIME_IMAGE}
ARG OCI_SOURCE=https://github.com/shiweijiezero/foretoken
ARG OCI_REVISION
LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"
COPY --from=builder /out/manager /manager
COPY --from=builder /out/crd-apply /crd-apply
# Ship the generated API definitions with the binaries that consume them.
COPY control-plane/config/crd/bases/ /opt/foretoken/crds/
COPY LICENSE /LICENSE
USER 65532:65532
ENTRYPOINT ["/manager"]
