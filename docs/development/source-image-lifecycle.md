<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Manual source image lifecycle

English | [简体中文](source-image-lifecycle_zh.md)

This maintainer guide covers manual image import and raw Helm operations. Run commands from the Foretoken repository root unless stated otherwise.

## Import local images directly

**Option 1: Import into a Kind cluster.** Create a Kind cluster directly to validate the control plane, CRDs, frontend, and scheduling behavior. To run a GPU model service, use k3d in option 2 and select the available GPUs as described in [Deploy Foretoken with k3d](../k3d-deployment.md). Install Kind first:

```bash
export KIND_VERSION=v0.32.0
mkdir -p ./tmp/bin
curl -fL \
  -o ./tmp/bin/kind \
  "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-linux-amd64"
chmod +x ./tmp/bin/kind
export PATH="$PWD/tmp/bin:$PATH"
kind version
```

Create a single-node cluster:

```bash
# Expected runtime: about 20 seconds
export KIND_CLUSTER=foretoken-local
kind create cluster --name "$KIND_CLUSTER"
```

If you need to simulate a multi-node topology on the same machine, use the Kind config included in the project.

```bash
# Expected runtime: about 30 seconds
export KIND_CLUSTER=foretoken-local
kind create cluster \
  --name "$KIND_CLUSTER" \
  --config deploy/kind/multi-node.yaml
```

After creating the cluster, build and import the local images.

```bash
# Expected runtime: about 8 minutes
make dev-build

kind load docker-image \
  --name "$KIND_CLUSTER" \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest

kubectl config use-context "kind-$KIND_CLUSTER"
kubectl get nodes
```

**Option 2: Import into a k3d cluster.** List the clusters on the current machine and set `CLUSTER` to the actual name:

```bash
k3d cluster list
export CLUSTER=your-cluster-name
```

If the target cluster has not been created, complete the cluster creation steps in [Deploy Foretoken with k3d](../k3d-deployment.md) first. Then build and import the local images from the repository root.

```bash
# Expected runtime: about 6 minutes
make dev-build

k3d image import --cluster "$CLUSTER" \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest

mkdir -p ./tmp
k3d kubeconfig get "$CLUSTER" \
  > "./tmp/kubeconfig-$CLUSTER.yaml"
export KUBECONFIG="$PWD/tmp/kubeconfig-$CLUSTER.yaml"
kubectl get nodes
```

`--namespace k8s.io` selects the containerd image namespace used by Kubernetes. A node administrator performs options 3 and 4.

**Option 3: Import into single-node containerd.** When the Kubernetes node and development machine are the same host, build the image bundle from the repository root and import it into the Kubernetes containerd namespace.

```bash
make dev-build
mkdir -p ./tmp

docker save \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest \
  --output ./tmp/foretoken-dev-images.tar

sudo ctr --namespace k8s.io images import ./tmp/foretoken-dev-images.tar
rm ./tmp/foretoken-dev-images.tar
```

**Option 4: Import into multi-node containerd.** For an offline multi-node Kubernetes cluster that uses containerd, build the image bundle on the development machine.

```bash
make dev-build
mkdir -p ./tmp

docker save \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest \
  --output ./tmp/foretoken-dev-images.tar
```

Replace `node-a` and `node-b` with the SSH addresses of the actual nodes, then import the bundle into every node that may run a Foretoken workload.

```bash
for NODE in node-a node-b; do
  # Transfer the image bundle to the node
  ssh "$NODE" 'mkdir -p ./tmp'
  rsync --archive --progress \
    ./tmp/foretoken-dev-images.tar \
    "$NODE:./tmp/foretoken-dev-images.tar"

  # Import it into the containerd image namespace used by Kubernetes
  ssh -t "$NODE" \
    'sudo ctr --namespace k8s.io images import ./tmp/foretoken-dev-images.tar &&
     rm ./tmp/foretoken-dev-images.tar'
done
```

## Install the platform with Helm

After importing the images, confirm that the current Kubernetes context points to the target cluster, then run the Helm command once.

```bash
# Expected runtime: about 30 seconds
helm upgrade --install foretoken \
  ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=local \
  --set image.repository=foretoken-dev-control-plane \
  --set image.tag=latest \
  --set image.pullPolicy=Never \
  --set frontend.image=foretoken-dev-frontend:latest \
  --set runtime.vllm.image=foretoken-dev-model-server:latest \
  --wait \
  --timeout=5m
```

## Build and deploy through an OCI registry

An OCI registry distributes images built on the development machine to Kubernetes nodes. The following example uses GHCR.

```bash
export GITHUB_USER=your-github-user
export REGISTRY="ghcr.io/$GITHUB_USER/foretoken-dev"
docker login ghcr.io
REGISTRY="$REGISTRY" make dev-deploy
```

This command pushes the images and installs or updates the Foretoken platform.

The script pushes:

```text
ghcr.io/your-github-user/foretoken-dev/control-plane:<tag>
ghcr.io/your-github-user/foretoken-dev/frontend:<tag>
ghcr.io/your-github-user/foretoken-dev/model-server:<tag>
```

For a private registry, provide a Kubernetes image pull Secret through `IMAGE_PULL_SECRET`:

```bash
REGISTRY="$REGISTRY" \
IMAGE_PULL_SECRET=foretoken-registry \
make dev-deploy
```
