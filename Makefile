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

# Inference backend compiled into the model-server binary. Defaults to vLLM;
# pass ENGINE_FEATURES=backend-sglang to build the SGLang adapter.
ENGINE_FEATURES ?= backend-vllm

# Base inference-engine images. Must be set explicitly to pin a reproducible
# engine version; there is intentionally no `latest` default.
VLLM_ENGINE_IMAGE ?=
SGLANG_ENGINE_IMAGE ?=

.PHONY: vllm-source build-data-plane verify-data-plane \
	image-frontend image-model-server image-model-server-vllm image-model-server-sglang image-benchmark

vllm-source:
	@test -f data-plane/third_party/vllm/rust/Cargo.toml || \
		git submodule update --init data-plane/third_party/vllm
	@if git -C data-plane/third_party/vllm apply --reverse --check \
		../../patches/vllm-chat-request-processor.patch >/dev/null 2>&1; then \
		:; \
	else \
		git -C data-plane/third_party/vllm apply \
			../../patches/vllm-chat-request-processor.patch; \
	fi

build-data-plane: vllm-source
	cargo build --manifest-path data-plane/Cargo.toml --workspace --locked

verify-data-plane: vllm-source
	cargo fmt --manifest-path data-plane/Cargo.toml $(DATA_PLANE_FMT_PACKAGES) -- --check
	cargo check --manifest-path data-plane/Cargo.toml --workspace --locked
	cargo test --manifest-path data-plane/Cargo.toml --workspace --locked
	cargo clippy --manifest-path data-plane/Cargo.toml --workspace --all-targets --locked -- -D warnings

dev-build:
	./deploy/dev-build

dev-deploy:
	./deploy/dev-deploy

image-frontend: vllm-source
	docker build -f data-plane/frontend/Dockerfile -t foretoken-frontend:dev .

image-model-server: vllm-source
	@test -n "$(INFERENCE_ENGINE_IMAGE)" || \
		(printf '%s\n' 'Set INFERENCE_ENGINE_IMAGE to a compatible inference engine image.' >&2; exit 1)
	docker build --build-arg INFERENCE_ENGINE_IMAGE="$(INFERENCE_ENGINE_IMAGE)" \
		--build-arg ENGINE_FEATURES="$(ENGINE_FEATURES)" \
		-f data-plane/model-server/Dockerfile \
		-t foretoken-model-server-$(ENGINE_FEATURES:backend-%=%):dev .

image-model-server-vllm:
	@test -n "$(VLLM_ENGINE_IMAGE)" || \
		(printf '%s\n' 'Set VLLM_ENGINE_IMAGE to a vLLM base image (e.g. vllm/vllm-openai:<version>).' >&2; exit 1)
	$(MAKE) image-model-server \
		INFERENCE_ENGINE_IMAGE="$(VLLM_ENGINE_IMAGE)" \
		ENGINE_FEATURES=backend-vllm

# SGLang builds still depend on vllm-source: the data-plane workspace resolves
# the vllm submodule path dependencies when Cargo parses, even though
# backend-sglang compiles no vLLM FFI.
image-model-server-sglang:
	@test -n "$(SGLANG_ENGINE_IMAGE)" || \
		(printf '%s\n' 'Set SGLANG_ENGINE_IMAGE to an SGLang base image (e.g. lmsysorg/sglang:<version>).' >&2; exit 1)
	$(MAKE) image-model-server \
		INFERENCE_ENGINE_IMAGE="$(SGLANG_ENGINE_IMAGE)" \
		ENGINE_FEATURES=backend-sglang

image-benchmark:
	docker build -f benchmarks/Dockerfile -t foretoken-benchmark:dev .
