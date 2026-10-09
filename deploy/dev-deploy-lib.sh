#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Build the three development images selected by deploy/dev-build.
build_dev_images() {
  export DOCKER_BUILDKIT=1

  local -a go_args=() cargo_args=() model_args=()
  local name value
  for name in GOPROXY GOSUMDB; do
    value=${!name:-}
    [[ -z "$value" ]] || go_args+=(--build-arg "$name=$value")
  done
  for name in \
    FORETOKEN_GITHUB_MIRROR \
    FORETOKEN_CARGO_REGISTRY \
    CARGO_NET_GIT_FETCH_WITH_CLI; do
    value=${!name:-}
    [[ -z "$value" ]] || cargo_args+=(--build-arg "$name=$value")
  done
  if [[ -n "${UV_DEFAULT_INDEX:-}" ]]; then
    model_args+=(--build-arg "UV_DEFAULT_INDEX=$UV_DEFAULT_INDEX")
  fi

  local -a control_plane_image_args=() data_plane_image_args=() model_image_args=()
  local docker_registry=${FORETOKEN_DOCKER_IO_REGISTRY:-${FORETOKEN_OCI_REGISTRY:-}}
  local gcr_registry=${FORETOKEN_GCR_REGISTRY:-${FORETOKEN_OCI_REGISTRY:-}}
  local ghcr_registry=${FORETOKEN_GHCR_REGISTRY:-${FORETOKEN_OCI_REGISTRY:-}}
  if [[ -n "$docker_registry" ]]; then
    control_plane_image_args+=(
      --build-arg "GO_IMAGE_REGISTRY=${docker_registry%/}"
    )
    data_plane_image_args+=(
      --build-arg "BASE_IMAGE_REGISTRY=${docker_registry%/}"
    )
  fi
  if [[ -n "$gcr_registry" ]]; then
    control_plane_image_args+=(
      --build-arg "DISTROLESS_IMAGE_REGISTRY=${gcr_registry%/}"
    )
  fi
  if [[ -n "$ghcr_registry" ]]; then
    model_image_args+=(
      --build-arg "INFERENCE_ENGINE_IMAGE_REGISTRY=${ghcr_registry%/}"
    )
    if [[ -z "${UV_IMAGE:-}" ]]; then
      model_image_args+=(
        --build-arg "UV_IMAGE_REGISTRY=${ghcr_registry%/}"
      )
    fi
  fi
  if [[ -n "${UV_IMAGE:-}" ]]; then
    model_image_args+=(--build-arg "UV_IMAGE=$UV_IMAGE")
  fi

  printf 'Building control-plane image: %s\n' "$CONTROL_PLANE_IMAGE"
  docker build \
    "${control_plane_image_args[@]}" \
    "${go_args[@]}" \
    -f control-plane/Dockerfile \
    -t "$CONTROL_PLANE_IMAGE" \
    .

  printf 'Preparing pinned vLLM build source\n'
  make vllm-source

  printf 'Building frontend image: %s\n' "$FRONTEND_IMAGE"
  docker build \
    "${data_plane_image_args[@]}" \
    "${cargo_args[@]}" \
    -f data-plane/frontend/Dockerfile \
    -t "$FRONTEND_IMAGE" \
    .

  printf 'Building model-server image: %s\n' "$MODEL_SERVER_IMAGE"
  docker build \
    "${data_plane_image_args[@]}" \
    "${model_image_args[@]}" \
    --build-arg INFERENCE_ENGINE_IMAGE \
    --build-arg FORETOKEN_VLLM_PYTHON \
    "${cargo_args[@]}" \
    "${model_args[@]}" \
    -f data-plane/model-server/Dockerfile \
    -t "$MODEL_SERVER_IMAGE" \
    .
}
