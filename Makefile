# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Provides build and verification entrypoints for the Rust data plane.

MOONCAKE_IMAGE ?= foretoken-mooncake:dev

VLLM_METAX_VERSION ?= 0.24.0
VLLM_METAX_IMAGE ?= foretoken-vllm-metax:$(VLLM_METAX_VERSION)

.PHONY: vllm-source build-data-plane format verify-data-plane dev-build dev-deploy \
	image-frontend image-vllm-metax image-model-server image-model-server-metax \
	image-benchmark dashboard

# Regenerates the Grafana dashboard shipped by the chart; needs the `dev` extra installed.
dashboard:
	python3 deploy/grafana/system_overview.py > deploy/charts/foretoken/files/grafana/foretoken-system-overview.json

vllm-source:
	@test -f data-plane/third_party/vllm/rust/Cargo.toml || \
		git submodule update --init data-plane/third_party/vllm
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
		-f data-plane/model-server/Dockerfile -t foretoken-model-server:dev .

image-model-server-metax: image-vllm-metax
	$(MAKE) image-model-server \
		INFERENCE_ENGINE_IMAGE="$(VLLM_METAX_IMAGE)"

image-benchmark:
	docker build -f benchmarks/Dockerfile -t foretoken-benchmark:dev .

.PHONY: mooncake-source image-mooncake
mooncake-source:
	git submodule update --init third_party/mooncake
	git -C third_party/mooncake submodule update --init extern/pybind11 extern/yalantinglibs
	@for patch in provider-registration client-lifecycle; do \
		if ! git -C third_party/mooncake apply --reverse --check \
			"../../deploy/mooncake/patches/$$patch.patch" >/dev/null 2>&1; then \
			git -C third_party/mooncake apply "../../deploy/mooncake/patches/$$patch.patch" || exit $$?; \
		fi; \
	done

image-mooncake: mooncake-source
	docker build $(if $(BUILD_JOBS),--build-arg BUILD_JOBS=$(BUILD_JOBS),) \
		-f deploy/mooncake/Dockerfile -t "$(MOONCAKE_IMAGE)" .
