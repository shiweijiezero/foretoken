# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Provides build and verification entrypoints for the Rust data plane.

.PHONY: vllm-source build-data-plane verify-data-plane \
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
	cargo fmt --manifest-path data-plane/Cargo.toml --all -- --check
	cargo check --manifest-path data-plane/Cargo.toml --workspace --locked
	cargo test --manifest-path data-plane/Cargo.toml --workspace --locked
	cargo clippy --manifest-path data-plane/Cargo.toml --workspace --all-targets --locked -- -D warnings

image-frontend: vllm-source
	docker build -f data-plane/frontend/Dockerfile -t foretoken-frontend:dev .

image-model-server: vllm-source
	@test -n "$(VLLM_RUNTIME_IMAGE)" || \
		(printf '%s\n' 'Set VLLM_RUNTIME_IMAGE to a compatible vLLM Python runtime image.' >&2; exit 1)
	docker build --build-arg VLLM_RUNTIME_IMAGE="$(VLLM_RUNTIME_IMAGE)" \
		-f data-plane/model-server/Dockerfile -t foretoken-model-server:dev .

image-benchmark:
	docker build -f benchmarks/Dockerfile -t foretoken-benchmark:dev .
