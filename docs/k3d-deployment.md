<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken with k3d

[English](k3d-deployment.md) | [中文](k3d-deployment_zh.md)

k3d runs the lightweight k3s Kubernetes distribution in Docker containers. It is well suited to creating an isolated, disposable Foretoken cluster on a shared GPU server while retaining standard Helm, CRDs, and Kubernetes APIs. All k3d cluster nodes run on one Docker host; use k3s or Kubernetes for deployments across physical machines.

After creating the k3d cluster, run `make dev-deploy` from the repository root. The script builds the source and imports changed local images into the current cluster; the Helm deployment, Ready wait, and request verification remain the same as for any other Kubernetes cluster.

## How k3d restricts physical GPUs

A standard Kubernetes Pod requests a GPU type and count:

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

Pods do not specify host GPU indices. When creating Kubernetes node containers, k3d can first limit which devices Docker exposes:

```text
Host physical GPUs 6 and 7
→ Docker --gpus '"device=6,7"'
→ k3d node container
→ k3s NVIDIA device plugin
→ Foretoken Pod
```

## Prerequisites

The host needs:

- Linux;
- an NVIDIA driver;
- NVIDIA Container Toolkit;
- Docker configured to use the NVIDIA runtime; and
- k3d, kubectl, and Helm.

## 1. Select GPUs and name the cluster

List GPUs:

```bash
nvidia-smi
```

The following example selects GPUs 6 and 7 and names the cluster `foretoken-qwen-test`:

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
```

Create a single-server cluster:

```bash
if k3d cluster get "$CLUSTER" >/dev/null 2>&1; then
  k3d cluster delete "$CLUSTER"
fi

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

Change to the Foretoken project directory:

```bash
cd /path/to/your/foretoken
```

### 4.1 Choose a deployment method

- **Use release images**: continue with [section 4.2: Local mode](#42-local-mode) or [section 4.3: Gateway mode](#43-gateway-mode).
- **Deploy from source**: complete [section 2.1: Import local images directly](custom-deployment.md#21-import-local-images-directly), [section 4: Deploy the Quick Start](custom-deployment.md#4-deploy-the-quick-start-optional), and [section 5: Send a request](custom-deployment.md#5-send-a-request-optional).

### 4.2 Local mode

Install Foretoken from release images and deploy the Quick Start:

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=local \
  --wait

kubectl apply --server-side -k examples/quickstart

kubectl wait --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m \
  frontendservice/quickstart-frontend \
  modelservice/quickstart-qwen3-0.6b
```

Read the address that k3s ServiceLB assigns to the frontend:

```bash
export FRONTEND_HOST="$(kubectl get service quickstart-frontend \
  --namespace foretoken-demo \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')"
export FRONTEND_URL="http://$FRONTEND_HOST:8080"
```

### 4.3 Gateway mode

First, set the public hostname in `examples/quickstart/frontend.yaml`:

```yaml
spec:
  hostname: foretoken.example.com
```

Install Envoy Gateway, then deploy Foretoken and the Quick Start from release images:

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait

helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=gateway \
  --set frontend.gateway.create=true \
  --wait

kubectl apply --server-side -k examples/quickstart

kubectl wait --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m \
  frontendservice/quickstart-frontend \
  modelservice/quickstart-qwen3-0.6b
```

Use the configured hostname:

```bash
export FRONTEND_URL=https://foretoken.example.com
```

### 4.4 Send an OpenAI API-compatible request

```bash
curl "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Reply with: Foretoken is ready"}],
    "max_tokens": 32,
    "temperature": 0
  }'
printf '\n'
```

Foretoken YAML continues to request standard `nvidia.com/gpu` resources; host GPU selection occurs when the k3d cluster is created.


## 5. Clean up

Delete the cluster:

```bash
k3d cluster delete "$CLUSTER"
```

Deleting the cluster stops every Pod in it, removes its Kubernetes resources, and releases the GPUs passed to the k3d node containers.
