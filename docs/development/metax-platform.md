<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Prepare Foretoken for MetaX GPUs

English | [简体中文](metax-platform_zh.md)

Install the Foretoken platform on a MetaX GPU cluster. For model deployment and requests, continue with [Deploy a model on MetaX GPUs](../metax-deployment.md).

## Before you start

The cluster needs Kubernetes 1.29 or later, MetaX drivers, and a device plugin publishing `metax-tech.com/gpu`. Prepare a model directory visible to the target nodes or a StorageClass for the model cache; see [Model storage](../model-storage.md). The frontend also needs a reachable LoadBalancer address, or a Gateway when using Gateway mode.

Install the [Foretoken CLI](../../cli/README.md#install-the-command-line-tool) and make sure `kubectl` points to the target cluster. The CLI needs Helm and cluster permissions to install the platform and its shared dependencies.

## Install release images

```bash
foretoken install
```

The CLI selects MetaX-compatible release images and installs the platform and required shared dependencies. It reports when the platform is ready. For Gateway access or custom settings, see [CLI installation](../../cli/README.md#install-the-kubernetes-platform).

## Install from source

To build Foretoken's images from a checkout, follow the [source deployment guide](../custom-deployment.md) to prepare build tools and install the CLI from that checkout. Then run from the repository root:

```bash
foretoken install -e .
```

This uses the chart's MetaX inference runtime image as the base for the source-built model-server. For a cluster other than local kind or k3d, follow the [source deployment guide](../custom-deployment.md#2-build-images-and-install-the-platform-from-source) to sign in to a node-reachable registry and provide `--registry`; a private registry also needs image pull Secrets.

### Build the inference runtime from an SDK

To build the inference engine as well, provide a Debian-based MACA/PyTorch SDK image with Python 3.12, PyTorch 2.10, matching torchaudio, and development headers. Match its SDK and driver using the [MetaX release matrix](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html).

Replace `<maca-sdk-image>` with that image and run from the repository root:

```bash
METAX_SDK_IMAGE=<maca-sdk-image> foretoken install -e .
```

The command builds the supported engine sources and applies their patches, including GLM-5.3 support, before packaging and installing Foretoken. Add `--registry` for a remote cluster. An explicit `runtime.vllm.image` in platform values selects an existing runtime instead of building from the SDK.

## Uninstall

Delete model deployments first, then run:

```bash
foretoken uninstall
```

CRDs and reused cluster resources are retained.
