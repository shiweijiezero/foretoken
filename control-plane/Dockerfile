# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Builds the control-plane manager and CRD bootstrapper container image.

FROM docker.io/library/golang:1.26.5-bookworm@sha256:1ecb7edf62a0408027bd5729dfd6b1b8766e578e8df93995b225dfd0944eb651 AS builder
WORKDIR /workspace/control-plane
COPY control-plane/go.mod control-plane/go.sum ./
RUN go mod download
COPY control-plane/ ./
# Build the long-running manager and the one-shot CRD bootstrapper from the same source revision.
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/manager ./cmd && \
    CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/crd-apply ./cmd/crd-apply

FROM gcr.io/distroless/static-debian13:nonroot@sha256:f7f8f729987ad0fdf6b05eeeae94b26e6a0f613bdf46feea7fc40f7bd72953e6
COPY --from=builder /out/manager /manager
COPY --from=builder /out/crd-apply /crd-apply
# Ship the generated API definitions with the binaries that consume them.
COPY control-plane/config/crd/bases/ /opt/foretoken/crds/
COPY LICENSE /LICENSE
USER 65532:65532
ENTRYPOINT ["/manager"]
