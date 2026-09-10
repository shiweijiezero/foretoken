# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Provides build and verification entrypoints for the Rust data plane.


# Inference backend compiled into the model-server binary. Defaults to vLLM;
# pass ENGINE_FEATURES=backend-sglang to build the SGLang adapter.
ENGINE_FEATURES ?= backend-vllm

# Base inference-engine images. Must be set explicitly.
VLLM_METAX_VERSION ?= 0.24.0
VLLM_METAX_IMAGE ?= foretoken-vllm-metax:$(VLLM_METAX_VERSION)
SGLANG_ENGINE_VERSION ?= 0.5.18
SGLANG_ENGINE_IMAGE ?= 

.PHONY: vllm-source build-data-plane format verify-data-plane dev-build dev-deploy \
	image-frontend image-vllm-metax image-model-server \
	image-model-server-sglang image-model-server-metax image-benchmark

# Patch list mirrors [workspace.metadata.foretoken].vllm_patches in
# data-plane/Cargo.toml; `cargo xtask` applies the same set for check/build.
vllm-source:
	@test -f data-plane/third_party/vllm/rust/Cargo.toml || \
		git submodule update --init data-plane/third_party/vllm
	@for patch in vllm-chat-request-processor vllm-engine-core-version-compatibility vllm-managed-engine-environment vllm-llm vllm-text; do \
		git -C data-plane/third_party/vllm apply --reverse --check "../../patches/$$patch.patch" >/dev/null 2>&1 || \
			git -C data-plane/third_party/vllm apply "../../patches/$$patch.patch" || exit 1; \
	done

build-data-plane: vllm-source
	cd data-plane && cargo xtask build

format:
	cd data-plane && cargo fmt --all

verify-data-plane: vllm-source
	cd data-plane && cargo xtask check

dev-build:
	./deploy/dev-build

dev-deploy:
	./deploy/dev-deploy

image-frontend: vllm-source
	docker build -f data-plane/frontend/Dockerfile -t foretoken-frontend:dev .

image-vllm-metax:
	@test -n "$(METAX_SDK_IMAGE)" || \
		(printf '%s\n' 'Set METAX_SDK_IMAGE to an Ubuntu/Debian image with the matching MACA SDK.' >&2; exit 1)
	docker build \
		--build-arg METAX_SDK_IMAGE="$(METAX_SDK_IMAGE)" \
		--build-arg MACA_PATH \
		--build-arg UV_IMAGE \
		--build-arg VLLM_VERSION="$(VLLM_METAX_VERSION)" \
		-t "$(VLLM_METAX_IMAGE)" deploy/inference-engines/vllm-metax

image-model-server: vllm-source
	@test -n "$(INFERENCE_ENGINE_IMAGE)" || \
		(printf '%s\n' 'Set INFERENCE_ENGINE_IMAGE to a compatible inference engine image.' >&2; exit 1)
	docker build --build-arg INFERENCE_ENGINE_IMAGE="$(INFERENCE_ENGINE_IMAGE)" \
		--build-arg FORETOKEN_VLLM_PYTHON \
		--build-arg ENGINE_FEATURES="$(ENGINE_FEATURES)" \
		-f data-plane/model-server/Dockerfile \
		-t foretoken-model-server-$(ENGINE_FEATURES:backend-%=%):dev .

image-model-server-sglang:
	@test -n "$(SGLANG_ENGINE_IMAGE)" || \
		(printf '%s\n' 'Set SGLANG_ENGINE_IMAGE' >&2; exit 1)
	$(MAKE) image-model-server \
		INFERENCE_ENGINE_IMAGE="$(SGLANG_ENGINE_IMAGE)" \
		ENGINE_FEATURES=backend-sglang

image-model-server-metax: image-vllm-metax
	$(MAKE) image-model-server \
		INFERENCE_ENGINE_IMAGE="$(VLLM_METAX_IMAGE)" \
		ENGINE_FEATURES=backend-vllm

image-benchmark:
	docker build -f benchmarks/Dockerfile -t foretoken-benchmark:dev .
