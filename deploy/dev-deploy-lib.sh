#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

build_dev_images() {
  export DOCKER_BUILDKIT=1

  docker build \
    -f control-plane/Dockerfile \
    -t "$CONTROL_PLANE_IMAGE" \
    .

  make vllm-source

  docker build \
    -f data-plane/frontend/Dockerfile \
    -t "$FRONTEND_IMAGE" \
    .

  docker build \
    --build-arg INFERENCE_ENGINE_IMAGE="$INFERENCE_ENGINE_IMAGE" \
    -f data-plane/model-server/Dockerfile \
    -t "$MODEL_SERVER_IMAGE" \
    .
}

create_k3d_cluster() {
  [[ -n "${GPU_INDICES:-}" ]] || {
    printf 'set GPU_INDICES to create k3d cluster %s\n' "$CLUSTER" >&2
    exit 1
  }
  if [[ ! -f "$K3D_CONFIG" && -f /usr/local/share/foretoken-k3d/config.yaml ]]; then
    K3D_CONFIG=/usr/local/share/foretoken-k3d/config.yaml
  fi
  [[ -f "$K3D_CONFIG" ]] || {
    printf 'k3d config not found: %s\n' "$K3D_CONFIG" >&2
    exit 1
  }

  local -a volume_args=()
  local -A mounted_paths=()
  add_mount() {
    local path=$1
    [[ -e "$path" ]] || return 0
    [[ -z "${mounted_paths[$path]+x}" ]] || return 0
    mounted_paths["$path"]=1
    volume_args+=(--volume "$path:$path@server:0")
  }

  local name tool_path path_kind library_path config_dir ldconfig_path
  for name in nvidia-container-runtime nvidia-container-runtime-hook nvidia-container-cli nvidia-ctk; do
    tool_path=$(command -v "$name")
    add_mount "$tool_path"
    while read -r path_kind library_path; do
      if [[ "$path_kind" == directory ]]; then
        add_mount "$(realpath -m "$(dirname "$library_path")")"
      else
        add_mount "$library_path"
      fi
    done < <(
      ldd "$tool_path" |
        awk '$2 == "=>" && $3 ~ /^\// { print "directory", $3 } $1 ~ /^\// { print "file", $1 }'
    )
  done
  for config_dir in /etc/nvidia-container-runtime /usr/local/etc/nvidia-container-runtime; do
    add_mount "$config_dir"
  done
  for ldconfig_path in "$(command -v ldconfig)" /sbin/ldconfig.real /usr/sbin/ldconfig.real; do
    add_mount "$ldconfig_path"
  done

  k3d cluster create "$CLUSTER" \
    --config "$K3D_CONFIG" \
    --gpus "\"device=$GPU_INDICES\"" \
    "${volume_args[@]}"
  CLUSTER_CREATED=true
}

image_id() {
  docker image inspect "$1" --format '{{.Id}}' 2>/dev/null || true
}

deployment_image() {
  kubectl get deployment \
    --namespace "$1" \
    --selector "$2" \
    -o jsonpath='{.items[0].spec.template.spec.containers[0].image}' \
    2>/dev/null || true
}

deployments_exist() {
  [[ -n "$(kubectl get deployment --namespace "$1" --selector "$2" -o name 2>/dev/null)" ]]
}

restart_deployments() {
  local namespace=$1 selector=$2
  deployments_exist "$namespace" "$selector" || return 0
  kubectl rollout restart deployment --namespace "$namespace" --selector "$selector"
  kubectl rollout status deployment --namespace "$namespace" --selector "$selector" --timeout "$DEV_TIMEOUT"
}

wait_for_deployment_image() {
  local namespace=$1 selector=$2 expected=$3 images
  deployments_exist "$namespace" "$selector" || return 0
  for _ in $(seq 1 60); do
    images=$(kubectl get deployment \
      --namespace "$namespace" \
      --selector "$selector" \
      -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].image}{"\n"}{end}')
    if [[ -n "$images" ]] && ! grep -Fvx "$expected" <<<"$images" >/dev/null; then
      kubectl rollout status deployment --namespace "$namespace" --selector "$selector" --timeout "$DEV_TIMEOUT"
      return 0
    fi
    sleep 2
  done
  printf 'deployment image did not update to %s\n' "$expected" >&2
  return 1
}
