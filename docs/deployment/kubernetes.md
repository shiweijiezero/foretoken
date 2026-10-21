<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken on Kubernetes

[English](kubernetes.md) | [中文](kubernetes_zh.md)

Use this guide to administer K3s, RKE2, KubeSphere, cloud Kubernetes, or another cluster that is not created by the local kind or k3d guides. To give members service access without platform permissions, prepare their [deployment namespaces](../../cli/README.md#prepare-a-namespace-for-members) after installing the platform.

## 1. Prepare the cluster

Use the installation guide for your platform to create the cluster. Then configure a kubeconfig on the machine where you run Foretoken and verify the active context:

```bash
kubectl config current-context
kubectl get nodes
```

The cluster needs:

- Kubernetes access through `kubectl` and [Helm 4](../../cli/README.md#before-you-start);
- a default StorageClass for platform persistent storage;
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
kubectl create namespace foretoken-platform --dry-run=client -o yaml | kubectl apply -f -
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

### Assign external service addresses

The default shared Gateway needs a LoadBalancer address. If the cluster already provides these addresses, use its existing implementation. When installation reports `LoadBalancer support Not verified`, Foretoken can configure MetalLB with unused addresses in the nodes' subnet. Obtain the range from the cluster administrator and add it to `deploy/platform-values.yaml`, replacing this example:

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

Apply it with the same installation settings:

```bash
foretoken install -e . --registry "$REGISTRY" --values deploy/platform-values.yaml
```

## 3. Deploy and update code

Use the platform's default model storage when it is configured. If none is prepared, or you want a separate volume, [request dynamic storage](../features/model-storage.md#use-dynamic-storage). To use a different shared directory, [select an administrator-authorized path](../features/model-storage.md#choose-a-directory).

Deploy and update code:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b -e . --timeout 20m
```

Without `--name`, the instance name comes from `metadata.name` of the `FrontendService` in [`frontend.yaml`](../../examples/quickstart/qwen3-0.6b/frontend.yaml): `qwen3-0-6b` in this example. It is deployed in the current kubeconfig namespace. Add `--name candidate-a` to use `candidate-a` instead, including in subsequent commands.

After editing controller, frontend or model-server code, run the same deployment command again to update only that instance. Shared CRD changes require `foretoken install -e . --registry "$REGISTRY"` first, retaining any installation overrides. See [Deploy Foretoken from Source](source.md#deploy-a-model-service) for engine checkouts and runtime changes.

## 4. Remove Foretoken

```bash
foretoken delete qwen3-0-6b
foretoken uninstall
```

These commands remove Foretoken resources but do not remove the Kubernetes cluster.
