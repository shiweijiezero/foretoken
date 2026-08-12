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
	FORETOKEN_VLLM_SOURCE_REVISION="$$(git -C third_party/vllm-rust/source rev-parse HEAD)" cargo build --manifest-path data-plane/frontend/Cargo.toml --workspace --locked

build-model-server: vllm-rust-source
	FORETOKEN_VLLM_SOURCE_REVISION="$$(git -C third_party/vllm-rust/source rev-parse HEAD)" cargo build --manifest-path data-plane/model-server/Cargo.toml --locked

verify-frontend: vllm-rust-source
	cargo fmt --manifest-path data-plane/frontend/Cargo.toml --all -- --check
	FORETOKEN_VLLM_SOURCE_REVISION="$$(git -C third_party/vllm-rust/source rev-parse HEAD)" cargo check --manifest-path data-plane/frontend/Cargo.toml --workspace --locked
	FORETOKEN_VLLM_SOURCE_REVISION="$$(git -C third_party/vllm-rust/source rev-parse HEAD)" cargo test --manifest-path data-plane/frontend/Cargo.toml --workspace --locked
	FORETOKEN_VLLM_SOURCE_REVISION="$$(git -C third_party/vllm-rust/source rev-parse HEAD)" cargo clippy --manifest-path data-plane/frontend/Cargo.toml --workspace --all-targets --locked -- -D warnings

verify-model-server: vllm-rust-source
	cargo fmt --manifest-path data-plane/model-server/Cargo.toml --all -- --check
	FORETOKEN_VLLM_SOURCE_REVISION="$$(git -C third_party/vllm-rust/source rev-parse HEAD)" cargo check --manifest-path data-plane/model-server/Cargo.toml --locked
	FORETOKEN_VLLM_SOURCE_REVISION="$$(git -C third_party/vllm-rust/source rev-parse HEAD)" cargo test --manifest-path data-plane/model-server/Cargo.toml --locked
	FORETOKEN_VLLM_SOURCE_REVISION="$$(git -C third_party/vllm-rust/source rev-parse HEAD)" cargo clippy --manifest-path data-plane/model-server/Cargo.toml --all-targets --locked -- -D warnings

verify-data-plane: verify-frontend verify-model-server

image-frontend: vllm-rust-source
	docker build -f data-plane/frontend/Dockerfile -t foretoken-frontend:dev .

image-model-server: vllm-rust-source
	@case "$(VLLM_RUNTIME_IMAGE)" in \
		*@sha256:[0-9a-f][0-9a-f]*) ;; \
		*) echo "VLLM_RUNTIME_IMAGE must use a digest-qualified sha256 reference" >&2; exit 1;; \
	esac
	@digest="$(VLLM_RUNTIME_IMAGE)"; digest="$${digest##*@sha256:}"; \
		test "$${#digest}" -eq 64 && ! printf '%s' "$$digest" | grep -q '[^0-9a-f]' || \
		(echo "VLLM_RUNTIME_IMAGE must use a 64-character lowercase sha256 digest" >&2; exit 1)
	docker build --build-arg VLLM_RUNTIME_IMAGE="$(VLLM_RUNTIME_IMAGE)" \
		--build-arg VLLM_SOURCE_REVISION="$$(git -C third_party/vllm-rust/source rev-parse HEAD)" \
		-f data-plane/model-server/Dockerfile -t foretoken-model-server:dev .
