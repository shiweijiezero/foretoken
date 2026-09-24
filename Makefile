# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Provides repository build, image, and verification entrypoints.

CONTROL_PLANE_IMAGE ?= foretoken-control-plane:dev
FRONTEND_IMAGE ?= foretoken-frontend:dev
MODEL_SERVER_IMAGE ?= foretoken-model-server:dev
OMNI_MODEL_SERVER_IMAGE ?= foretoken-omni-model-server:dev
MOONCAKE_IMAGE ?= foretoken-mooncake
MOONCAKE_VERSION ?=

OCI_REGISTRY := $(patsubst %/,%,$(strip $(FORETOKEN_OCI_REGISTRY)))
OCI_SOURCE ?= https://github.com/shiweijiezero/foretoken
OCI_REVISION ?= $(shell git rev-parse HEAD)

VLLM_METAX_VERSION ?= 0.24.0
VLLM_METAX_IMAGE ?= foretoken-vllm-metax:$(VLLM_METAX_VERSION)

GIT = git $(if $(FORETOKEN_GITHUB_MIRROR),-c url.$(patsubst %/,%,$(FORETOKEN_GITHUB_MIRROR))/.insteadOf=https://github.com/,)

.PHONY: vllm-source build-data-plane format verify-data-plane dev-build dev-deploy \
	image-control-plane image-frontend image-vllm-metax image-model-server image-model-server-omni \
	image-model-server-metax image-benchmark dashboard

# Regenerates the localized Grafana dashboards shipped by the chart; needs the `dev` extra installed.
dashboard:
	python3 deploy/grafana/system_overview.py --locale en > deploy/charts/foretoken/files/grafana/foretoken-system-overview.json
	python3 deploy/grafana/system_overview.py --locale zh > deploy/charts/foretoken/files/grafana/foretoken-system-overview-zh.json

vllm-source:
	@test -f data-plane/third_party/vllm/rust/Cargo.toml || \
		$(GIT) submodule update --init data-plane/third_party/vllm
	cd data-plane && cargo xtask prepare-vllm

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

image-control-plane:
	docker build \
		$(if $(OCI_REGISTRY),--build-arg GO_IMAGE_REGISTRY="$(OCI_REGISTRY)",) \
		$(if $(OCI_REGISTRY),--build-arg DISTROLESS_IMAGE_REGISTRY="$(OCI_REGISTRY)",) \
		--build-arg GOPROXY \
		--build-arg GOSUMDB \
		--build-arg OCI_SOURCE="$(OCI_SOURCE)" \
		--build-arg OCI_REVISION="$(OCI_REVISION)" \
		-f control-plane/Dockerfile -t "$(CONTROL_PLANE_IMAGE)" .

image-frontend: vllm-source
	docker build \
		$(if $(OCI_REGISTRY),--build-arg BASE_IMAGE_REGISTRY="$(OCI_REGISTRY)",) \
		--build-arg FORETOKEN_GITHUB_MIRROR \
		--build-arg FORETOKEN_CARGO_REGISTRY \
		--build-arg CARGO_NET_GIT_FETCH_WITH_CLI \
		--build-arg OCI_SOURCE="$(OCI_SOURCE)" \
		--build-arg OCI_REVISION="$(OCI_REVISION)" \
		-f data-plane/frontend/Dockerfile -t "$(FRONTEND_IMAGE)" .

image-vllm-metax: mooncake-source
	@test -n "$(METAX_SDK_IMAGE)" || \
		(printf '%s\n' 'Set METAX_SDK_IMAGE to an Ubuntu/Debian image with the matching MACA SDK.' >&2; exit 1)
	docker build \
		--build-arg METAX_SDK_IMAGE="$(METAX_SDK_IMAGE)" \
		--build-arg MACA_PATH \
		$(if $(BUILD_JOBS),--build-arg BUILD_JOBS="$(BUILD_JOBS)",) \
		$(if $(OCI_REGISTRY),--build-arg UV_IMAGE_REGISTRY="$(OCI_REGISTRY)",) \
		$(if $(UV_IMAGE),--build-arg UV_IMAGE="$(UV_IMAGE)",) \
		--build-arg FORETOKEN_GITHUB_MIRROR \
		--build-arg UV_DEFAULT_INDEX \
		--build-arg UV_EXTRA_INDEX_URL \
		--build-arg VLLM_VERSION="$(VLLM_METAX_VERSION)" \
		-f deploy/inference-engines/vllm-metax/Dockerfile \
		-t "$(VLLM_METAX_IMAGE)" .

image-model-server: vllm-source
	@test -n "$(INFERENCE_ENGINE_IMAGE)" || \
		(printf '%s\n' 'Set INFERENCE_ENGINE_IMAGE to a compatible inference engine image.' >&2; exit 1)
	docker build --build-arg INFERENCE_ENGINE_IMAGE="$(INFERENCE_ENGINE_IMAGE)" \
		$(if $(OCI_REGISTRY),--build-arg BASE_IMAGE_REGISTRY="$(OCI_REGISTRY)",) \
		$(if $(OCI_REGISTRY),--build-arg UV_IMAGE_REGISTRY="$(OCI_REGISTRY)",) \
		$(if $(UV_IMAGE),--build-arg UV_IMAGE="$(UV_IMAGE)",) \
		--build-arg FORETOKEN_VLLM_PYTHON \
		--build-arg FORETOKEN_GITHUB_MIRROR \
		--build-arg FORETOKEN_CARGO_REGISTRY \
		--build-arg CARGO_NET_GIT_FETCH_WITH_CLI \
		--build-arg UV_DEFAULT_INDEX \
		--build-arg OCI_SOURCE="$(OCI_SOURCE)" \
		--build-arg OCI_REVISION="$(OCI_REVISION)" \
		-f data-plane/model-server/Dockerfile -t "$(MODEL_SERVER_IMAGE)" .

image-model-server-omni: vllm-source
	@test -n "$(INFERENCE_ENGINE_IMAGE)" || \
		(printf '%s\n' 'Set INFERENCE_ENGINE_IMAGE to a compatible vLLM-Omni image.' >&2; exit 1)
	docker build --target omni-runtime \
		--build-arg INFERENCE_ENGINE_IMAGE="$(INFERENCE_ENGINE_IMAGE)" \
		--build-arg FORETOKEN_MODEL_SERVER_BINARY=foretoken-omni-model-server \
		$(if $(OMNI_VIDEO_SYNC_TIMEOUT),--build-arg OMNI_VIDEO_SYNC_TIMEOUT="$(OMNI_VIDEO_SYNC_TIMEOUT)",) \
		$(if $(OCI_REGISTRY),--build-arg BASE_IMAGE_REGISTRY="$(OCI_REGISTRY)",) \
		--build-arg FORETOKEN_GITHUB_MIRROR \
		--build-arg FORETOKEN_CARGO_REGISTRY \
		--build-arg CARGO_NET_GIT_FETCH_WITH_CLI \
		--build-arg OCI_SOURCE="$(OCI_SOURCE)" \
		--build-arg OCI_REVISION="$(OCI_REVISION)" \
		-f data-plane/model-server/Dockerfile -t "$(OMNI_MODEL_SERVER_IMAGE)" .

image-model-server-metax: image-vllm-metax
	$(MAKE) image-model-server \
		INFERENCE_ENGINE_IMAGE="$(VLLM_METAX_IMAGE)"

image-benchmark:
	docker build -f benchmarks/Dockerfile -t foretoken-benchmark:dev .

.PHONY: mooncake-source image-mooncake
mooncake-source:
	MOONCAKE_VERSION="$(MOONCAKE_VERSION)" \
		FORETOKEN_GITHUB_MIRROR="$(FORETOKEN_GITHUB_MIRROR)" \
		./deploy/mooncake/prepare-source

image-mooncake: mooncake-source
	docker build $(if $(BUILD_JOBS),--build-arg BUILD_JOBS=$(BUILD_JOBS),) \
		$(if $(OCI_REGISTRY),--build-arg BASE_IMAGE_REGISTRY="$(OCI_REGISTRY)",) \
		$(if $(MOONCAKE_BUILD_IMAGE),--build-arg BUILD_IMAGE="$(MOONCAKE_BUILD_IMAGE)",) \
		$(if $(MOONCAKE_RUNTIME_IMAGE),--build-arg RUNTIME_IMAGE="$(MOONCAKE_RUNTIME_IMAGE)",) \
		$(if $(MOONCAKE_GO_IMAGE),--build-arg GO_IMAGE="$(MOONCAKE_GO_IMAGE)",) \
		--build-arg GOPROXY \
		--build-arg GOSUMDB \
		-f deploy/mooncake/Dockerfile -t "$(MOONCAKE_IMAGE)" .
