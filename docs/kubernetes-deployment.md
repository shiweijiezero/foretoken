<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken on Kubernetes

[English](kubernetes-deployment.md) | [中文](kubernetes-deployment_zh.md)

Use this guide for K3s, RKE2, KubeSphere, cloud Kubernetes, or another cluster that is not created by the local kind or k3d guides.

## 1. Prepare the cluster

Use the installation guide for your platform to create the cluster. Then configure a kubeconfig on the machine where you run Foretoken and verify the active context:

```bash
kubectl config current-context
kubectl get nodes
```

The cluster needs:

- Kubernetes access through `kubectl` and Helm;
- a default StorageClass for compiler caches;
- GPU drivers and the vendor device plugin when deploying GPU models; and
- a registry reachable by the Build Pods and every node when using source-built images.

K3s single-server example:

```bash
curl -sfL https://get.k3s.io | sh -
mkdir -p "$HOME/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"
export KUBECONFIG="$HOME/.kube/config"
kubectl get nodes
```

For RKE2, KubeSphere, and cloud Kubernetes, use the platform's cluster and kubeconfig instructions, then continue here.

## 2. Install Foretoken from source

From the Foretoken repository root:

```bash
pip install -e .
export REGISTRY=registry.example.com:5000/foretoken
foretoken install -e . --registry "$REGISTRY"
```

Use a registry without authentication for an internal network when available. If the registry requires authentication, log in and create `registry-auth` before installation and in each workload namespace:

```bash
docker login registry.example.com:5000
kubectl create secret generic registry-auth \
  --namespace foretoken-platform \
  --from-file=.dockerconfigjson="$HOME/.docker/config.json" \
  --type=kubernetes.io/dockerconfigjson
```

Add the same Secret to each workload namespace and save these references in `deploy/platform-values.yaml`:

```yaml
imagePullSecrets:
  - name: registry-auth
workload:
  imagePullSecrets:
    - name: registry-auth
```

Install with `--values deploy/platform-values.yaml` when using the authenticated registry.

## 3. Deploy and update code

The Quick Start uses the repository's `data/` directory on local k3d. On a remote cluster, Foretoken automatically provisions a dynamic PVC for this relative directory, so the example YAML does not need to change. Multi-node clusters use `ReadWriteMany`; a single-node cluster uses `ReadWriteOnce` automatically. Set `initialSize`, `storageClassName`, `accessMode`, or `maxSize` in `cache.yaml` when the cluster needs a different storage policy.

Deploy and update code:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

After editing the checkout, run the same command again. Source changes are compiled in BuildKit Pods and published to the workloads; ordinary Python, Triton, Rust, CUDA, and C/C++ changes reuse the runtime image. Continue with [Deploy Foretoken from Source](custom-deployment.md#deploy-and-update-code) for engine checkouts and runtime changes.

## 4. Remove Foretoken

```bash
foretoken delete examples/quickstart
foretoken uninstall
```

These commands remove Foretoken resources but do not remove the Kubernetes cluster.
