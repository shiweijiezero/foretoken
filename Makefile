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

.PHONY: vllm-source build-data-plane verify-data-plane dev-build dev-deploy \
	image-frontend image-model-server image-benchmark

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
		-f data-plane/model-server/Dockerfile -t foretoken-model-server:dev .

image-benchmark:
	docker build -f benchmarks/Dockerfile -t foretoken-benchmark:dev .
