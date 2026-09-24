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

### Use a custom MetaX SDK image

If the inference runtime must be built against a different MACA SDK, provide a compatible Ubuntu 24.04 or Debian-based SDK image. It needs Python 3.12 and development headers. Match its SDK and driver using the [MetaX release matrix](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html).

From the repository root, replace `<maca-sdk-image>` with that image and build the inference runtime:

```bash
METAX_SDK_IMAGE=<maca-sdk-image> \
VLLM_METAX_IMAGE=foretoken-vllm-metax:custom \
make image-vllm-metax
```

To select a different supported engine version, set `VLLM_METAX_VERSION` on the build command and use a compatible SDK image. Save this override as `metax-values.yaml`; if you already have a compatible inference runtime image, skip the build and put its reference here instead:

```yaml
runtime:
  vllm:
    image: foretoken-vllm-metax:custom
```

```bash
foretoken install -e . --values metax-values.yaml
```

The source install builds the Foretoken model-server on the selected inference image, then distributes the Foretoken images and installs the platform. On a remote cluster, add `--registry` as described above. For manual image import or Helm operations instead, see the [source image lifecycle guide](source-image-lifecycle.md).

## Uninstall

Delete model deployments first, then run:

```bash
foretoken uninstall
```

CRDs and reused cluster resources are retained.
