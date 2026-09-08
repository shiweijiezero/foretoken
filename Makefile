# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Provides build and verification entrypoints for the Rust data plane.

DATA_PLANE_PACKAGES := \
	foretoken-backend-registry \
	foretoken-chat \
	foretoken-frontend \
	foretoken-kv-indexer \
	foretoken-llm-facade \
	foretoken-metrics \
	foretoken-model-protocol \
	foretoken-model-server \
	foretoken-parser \
	foretoken-router \
	foretoken-runtime-builder \
	foretoken-server \
	foretoken-text \
	foretoken-tokenizer \
	foretoken-tracing
DATA_PLANE_FMT_PACKAGES := $(foreach package,$(DATA_PLANE_PACKAGES),--package $(package))

VLLM_METAX_VERSION ?= 0.24.0
VLLM_METAX_IMAGE ?= foretoken-vllm-metax:$(VLLM_METAX_VERSION)
VLLM_METAX_PYTHON ?= /opt/foretoken-vllm/bin/python
MACA_PATH ?= /opt/maca
METAX_PYTHON ?= /opt/conda/bin/python
UV_VERSION ?= 0.9.10
PYPI_INDEX_URL ?= https://pypi.org/simple
METAX_INDEX_URL ?= https://repos.metax-tech.com/r/maca-pypi/simple

.PHONY: vllm-source build-data-plane verify-data-plane dev-build dev-deploy \
	image-frontend image-vllm-metax image-model-server image-model-server-metax \
	image-benchmark

vllm-source:
	@test -f data-plane/third_party/vllm/rust/Cargo.toml || \
		git submodule update --init data-plane/third_party/vllm
	@set -e; for patch in \
		vllm-chat-request-processor.patch \
		vllm-engine-core-version-compatibility.patch \
		vllm-managed-engine-environment.patch; do \
		if ! git -C data-plane/third_party/vllm apply --reverse --check \
			"../../patches/$$patch" >/dev/null 2>&1; then \
			git -C data-plane/third_party/vllm apply "../../patches/$$patch"; \
		fi; \
	done

build-data-plane: vllm-source
	cargo build --manifest-path data-plane/Cargo.toml --workspace --locked

verify-data-plane: vllm-source
	cargo fmt --manifest-path data-plane/Cargo.toml $(DATA_PLANE_FMT_PACKAGES) -- --check
	cargo test --manifest-path data-plane/Cargo.toml --workspace --locked
	cargo clippy --manifest-path data-plane/Cargo.toml --workspace --all-targets --locked -- -D warnings

dev-build:
	./deploy/dev-build

dev-deploy:
	./deploy/dev-deploy

image-frontend: vllm-source
	docker build -f data-plane/frontend/Dockerfile -t foretoken-frontend:dev .

image-vllm-metax:
	@test -n "$(METAX_BASE_IMAGE)" || \
		(printf '%s\n' 'Set METAX_BASE_IMAGE to a matching released MetaX vLLM image.' >&2; exit 1)
	docker build \
		--build-arg METAX_BASE_IMAGE="$(METAX_BASE_IMAGE)" \
		--build-arg MACA_PATH \
		--build-arg METAX_PYTHON \
		--build-arg UV_VERSION \
		--build-arg VLLM_VERSION="$(VLLM_METAX_VERSION)" \
		--build-arg PYPI_INDEX_URL \
		--build-arg METAX_INDEX_URL \
		-f deploy/inference-engines/vllm-metax/Dockerfile \
		-t "$(VLLM_METAX_IMAGE)" .

image-model-server: vllm-source
	@test -n "$(INFERENCE_ENGINE_IMAGE)" || \
		(printf '%s\n' 'Set INFERENCE_ENGINE_IMAGE to a compatible inference engine image.' >&2; exit 1)
	docker build --build-arg INFERENCE_ENGINE_IMAGE="$(INFERENCE_ENGINE_IMAGE)" \
		--build-arg FORETOKEN_VLLM_PYTHON \
		-f data-plane/model-server/Dockerfile -t foretoken-model-server:dev .

image-model-server-metax: image-vllm-metax
	$(MAKE) image-model-server \
		INFERENCE_ENGINE_IMAGE="$(VLLM_METAX_IMAGE)" \
		FORETOKEN_VLLM_PYTHON="$(VLLM_METAX_PYTHON)"

image-benchmark:
	docker build -f benchmarks/Dockerfile -t foretoken-benchmark:dev .
