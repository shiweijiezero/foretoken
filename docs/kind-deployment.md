<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken with kind

[English](kind-deployment.md) | [中文](kind-deployment_zh.md)

Use kind for a local Kubernetes development cluster. kind runs Kubernetes nodes in containers and does not provide GPU devices; use [k3d deployment](k3d-deployment.md) for a single-host GPU cluster.

## 1. Install the tools and create a cluster

Install Docker, kind, kubectl, Python 3.11+, and Helm. From the Foretoken repository root:

```bash
pip install -e .
foretoken cluster create kind --name foretoken-dev
kubectl get nodes
```

The command creates the kind cluster, refreshes its kubeconfig context, and waits for the control plane.

## 2. Build and install Foretoken

kind loads source-built images directly into its node containerd; no registry is needed:

```bash
foretoken install -e .
```

The Quick Start model requires a GPU; use the [k3d deployment guide](k3d-deployment.md) for that path. Use this kind cluster to validate platform installation and CPU-compatible workloads.

## 3. Remove the cluster

```bash
foretoken delete examples/quickstart
foretoken uninstall
foretoken cluster delete kind --name foretoken-dev
```

For an existing K3s, RKE2, KubeSphere, cloud, or other Kubernetes cluster, use the [Kubernetes deployment guide](kubernetes-deployment.md).
