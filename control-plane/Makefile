# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
#
# Defines control-plane build, validation, and code-generation targets.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

LOCALBIN := $(CURDIR)/bin
CONTROLLER_TOOLS_VERSION := v0.21.0
CONTROLLER_GEN := $(LOCALBIN)/controller-gen-$(CONTROLLER_TOOLS_VERSION)
CRD_BASE_DIR := config/crd/bases

.PHONY: all
all: build

.PHONY: manifests
manifests: $(CONTROLLER_GEN)
	$(CONTROLLER_GEN) crd:headerFile="hack/boilerplate.yaml.txt" paths="./..." output:crd:artifacts:config=$(CRD_BASE_DIR)

.PHONY: generate
generate: $(CONTROLLER_GEN)
	$(CONTROLLER_GEN) object:headerFile="hack/boilerplate.go.txt" paths="./..."

.PHONY: fmt
fmt:
	go fmt ./...

.PHONY: vet
vet:
	go vet ./...

.PHONY: test
test:
	go test ./...

.PHONY: build
build: generate manifests fmt vet
	go build -o $(LOCALBIN)/manager ./cmd
	go build -o $(LOCALBIN)/crd-apply ./cmd/crd-apply

.PHONY: verify
verify: generate manifests fmt vet test

$(CONTROLLER_GEN):
	@mkdir -p $(LOCALBIN)
	GOBIN=$(LOCALBIN) go install sigs.k8s.io/controller-tools/cmd/controller-gen@$(CONTROLLER_TOOLS_VERSION)
	mv $(LOCALBIN)/controller-gen $(CONTROLLER_GEN)
