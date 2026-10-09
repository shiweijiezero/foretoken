<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Prepare Foretoken for MetaX GPUs

English | [简体中文](metax-platform_zh.md)

Install the Foretoken platform on a MetaX GPU cluster. For model deployment and requests, continue with [Deploy a model on MetaX GPUs](../metax-deployment.md).

## Before you start

Use Kubernetes 1.29 or later with MetaX drivers, a GPU-enabled container runtime, and a device plugin exposing `metax-tech.com/gpu` resources.

Provide a model directory accessible to the target nodes or a StorageClass; see [Model storage](../model-storage.md). The frontend needs a reachable LoadBalancer or Gateway endpoint.

Install the [Foretoken CLI](../../cli/README.md#install-the-command-line-tool) and make sure `kubectl` points to the target cluster. The CLI needs Helm and cluster permissions to install the platform and its shared dependencies.

## Install release images

```bash
foretoken install
```

The CLI selects MetaX-compatible release images and installs the platform and required shared dependencies. It reports when the platform is ready. For Gateway access or custom settings, see [CLI installation](../../cli/README.md#install-the-kubernetes-platform).

## Install from source

From the repository root, build and install the platform in the cluster:

```bash
foretoken install -e .
```

The build automatically prepares the MetaX inference runtime. See the [source deployment guide](../custom-deployment.md#install-from-source) for image import and registry distribution.

To use your own SDK image, set `METAX_SDK_IMAGE` when running the command. To reuse an existing inference runtime instead of rebuilding it, set `runtime.vllm.image` in platform values supplied through `--values`.

## Uninstall

Delete model deployments first, then run:

```bash
foretoken uninstall
```

CRDs and reused cluster resources are retained.
