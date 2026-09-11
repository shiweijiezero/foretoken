<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken with k3d

[English](k3d-deployment.md) | [中文](k3d-deployment_zh.md)

k3d runs the lightweight k3s Kubernetes distribution in Docker containers. It is well suited to creating an isolated, disposable Foretoken cluster on a shared GPU server while retaining standard Helm, CRDs, and Kubernetes APIs. All k3d cluster nodes run on one Docker host; use k3s or Kubernetes for deployments across physical machines.

## Prerequisites

The host needs:

- Python 3.11 or later;
- Linux;
- an NVIDIA driver;
- NVIDIA Container Toolkit;
- Docker configured to use the NVIDIA runtime; and
- k3d, kubectl, and Helm.

## 1. Enter the repository and select GPUs

Get the repository and run the remaining commands from its root:

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

List GPUs:

```bash
nvidia-smi
```

Select GPUs without other workloads. The Quick Start needs one GPU, 8 CPU, and 52 GiB memory; allow additional capacity for the platform. The following uses GPUs 6 and 7 and the cluster name `foretoken-qwen-test`; change them to your available devices. Docker limits the physical GPUs visible to the node, and Pods request a count from that set:

```bash
export GPU_INDICES=6,7
export CLUSTER=foretoken-qwen-test
```

## 2. Create a GPU-restricted k3d cluster

The following Bash code finds the NVIDIA runtime, configuration, and dependent libraries, then prepares mount arguments for k3d:

```bash
declare -a K3D_VOLUME_ARGS=()
declare -A K3D_MOUNTED_PATHS=()

add_k3d_mount() {
  local path="$1"
  [ -e "$path" ] || return 0
  [ -z "${K3D_MOUNTED_PATHS[$path]+x}" ] || return 0
  K3D_MOUNTED_PATHS["$path"]=1
  K3D_VOLUME_ARGS+=(--volume "$path:$path@server:0")
}

for NAME in \
  nvidia-container-runtime \
  nvidia-container-runtime-hook \
  nvidia-container-cli \
  nvidia-ctk; do
  TOOL_PATH="$(command -v "$NAME")"
  add_k3d_mount "$TOOL_PATH"

  while read -r PATH_KIND LIBRARY_PATH; do
    if [ "$PATH_KIND" = directory ]; then
      add_k3d_mount "$(realpath -m "$(dirname "$LIBRARY_PATH")")"
    else
      add_k3d_mount "$LIBRARY_PATH"
    fi
  done < <(
    ldd "$TOOL_PATH" |
      awk '
        $2 == "=>" && $3 ~ /^\// { print "directory", $3 }
        $1 ~ /^\// { print "file", $1 }
      '
  )
done

for CONFIG_DIR in \
  /etc/nvidia-container-runtime \
  /usr/local/etc/nvidia-container-runtime; do
  add_k3d_mount "$CONFIG_DIR"
done

for LDCONFIG_PATH in \
  "$(command -v ldconfig)" \
  /sbin/ldconfig.real \
  /usr/sbin/ldconfig.real; do
  add_k3d_mount "$LDCONFIG_PATH"
done

# Keep model downloads and runtime caches in the example directory.
mkdir -p examples/quickstart/data
add_k3d_mount "$(realpath examples/quickstart/data)"
```

Give the frontend and model-server users write access to `examples/quickstart/data`; the standard frontend runs as UID/GID 65532. See [Model storage](model-storage.md) for other storage choices.

Create a single-server cluster:

```bash
k3d cluster create "$CLUSTER" \
  --config deploy/k3d/config.yaml \
  --gpus "\"device=$GPU_INDICES\"" \
  "${K3D_VOLUME_ARGS[@]}"
```

View the resulting nodes:

```bash
kubectl get nodes
```

## 3. Install the NVIDIA device plugin

```bash
kubectl apply -f \
  https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.4/deployments/static/nvidia-device-plugin.yml
```

Configure the inner NVIDIA runtime with the same host GPU list that k3d uses:

```bash
kubectl set env daemonset/nvidia-device-plugin-daemonset \
  --namespace kube-system \
  NVIDIA_VISIBLE_DEVICES="$GPU_INDICES"

kubectl rollout status daemonset/nvidia-device-plugin-daemonset \
  --namespace kube-system \
  --timeout=3m
```

## 4. Install and access Foretoken

### 4.1 Choose a deployment method

Build from this checkout to use the directory-backed example below. Prepare the tools listed in the [source deployment guide](custom-deployment.md), then run:

```bash
pip install -e .
foretoken install -e .
```

To use published packages and images instead, get the examples from the chosen [release](https://github.com/shiweijiezero/foretoken/releases) and install:

```bash
pip install foretoken
foretoken install
```

### 4.2 Local mode

Deploy the Quick Start and resolve the address assigned by k3s ServiceLB:

```bash
foretoken deploy examples/quickstart --timeout 20m
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"
```

### 4.3 Gateway mode

First, set the public hostname in `examples/quickstart/frontend.yaml`:

```yaml
spec:
  hostname: foretoken.example.com
```

Enable Gateway mode and deploy the Quick Start. The command installs Envoy Gateway when needed:

```bash
foretoken install -e . --frontend-mode gateway
# For a release installation: foretoken install --frontend-mode gateway
foretoken deploy examples/quickstart --timeout 20m
```

Resolve the configured Gateway endpoint:

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"
```

### 4.4 Send an OpenAI API-compatible request

```bash
curl "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Reply with: Foretoken is ready"}],
    "max_tokens": 32,
    "temperature": 0
  }'
printf '\n'
```

## 5. Clean up

Delete the cluster:

```bash
k3d cluster delete "$CLUSTER"
```

Deleting the cluster stops its Pods and releases the GPUs. Keep `examples/quickstart/data`; restore its bind mount when creating another cluster to reuse the downloaded models.
