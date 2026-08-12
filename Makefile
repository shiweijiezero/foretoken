# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Provides data-plane build entrypoints using a compatible local vLLM source.

.PHONY: vllm-rust-source \
	build-frontend build-model-server \
	verify-frontend verify-model-server verify-data-plane \
	image-frontend image-model-server

vllm-rust-source:
	./scripts/select-vllm-source.sh

build-frontend: vllm-rust-source
	cargo build --manifest-path data-plane/frontend/Cargo.toml --workspace --locked

build-model-server: vllm-rust-source
	cargo build --manifest-path data-plane/model-server/Cargo.toml --locked

verify-frontend: vllm-rust-source
	cargo fmt --manifest-path data-plane/frontend/Cargo.toml --all -- --check
	cargo check --manifest-path data-plane/frontend/Cargo.toml --workspace --locked
	cargo test --manifest-path data-plane/frontend/Cargo.toml --workspace --locked
	cargo clippy --manifest-path data-plane/frontend/Cargo.toml --workspace --all-targets --locked -- -D warnings

verify-model-server: vllm-rust-source
	cargo fmt --manifest-path data-plane/model-server/Cargo.toml --all -- --check
	cargo check --manifest-path data-plane/model-server/Cargo.toml --locked
	cargo test --manifest-path data-plane/model-server/Cargo.toml --locked
	cargo clippy --manifest-path data-plane/model-server/Cargo.toml --all-targets --locked -- -D warnings

verify-data-plane: verify-frontend verify-model-server

image-frontend: vllm-rust-source
	docker build -f data-plane/frontend/Dockerfile -t foretoken-frontend:dev .

image-model-server: vllm-rust-source
	docker build --build-arg VLLM_RUNTIME_IMAGE="$(VLLM_RUNTIME_IMAGE)" \
		-f data-plane/model-server/Dockerfile -t foretoken-model-server:dev .
