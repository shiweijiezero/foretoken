<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Model Artifact Cache

English | [简体中文](model-artifact-cache_zh.md)

The optional model cache separates Hugging Face snapshot preparation from serving. The platform administrator supplies an existing namespace-local PVC that every workload node can mount; multi-node deployments normally require `ReadWriteMany`. ModelService owns the preparation Job and status; the platform owns cache retention; Frontend and model-server consume the prepared files.

## Ownership and lifecycle

```text
ModelService
  → preparation Job
  → ArtifactsReady
  → ModelPools
  → ModelGroups
```

The ModelService controller does not change target ModelPools until the Job completes. An existing serving generation remains selected while new artifacts are preparing. A failed Job publishes `ArtifactsReady=False` with reason `PreparationFailed`.

The Job uses the configured model-server image and the `foretoken-prepare-hf-snapshot` command. It receives the optional endpoint and token Secret, writes the standard Hugging Face cache layout, and exits. Long-running Frontend and model-server Pods receive neither the endpoint nor the token; they mount the cache and run with `HF_HUB_OFFLINE=1`.

The PVC is not owned or deleted by ModelService. This allows snapshots to survive service deletion and be reused by other services. Storage capacity, access mode, backup, and cache cleanup remain platform responsibilities.

## Runtime boundary

`ModelService.spec.model` remains the public model identity and Hugging Face repository ID. The first implementation keeps the standard Hugging Face cache layout instead of exposing internal artifact paths. Frontend resolves local directories, then the local cache, and contacts the Hub only when managed caching is disabled.

Do not add automatic PVC sizing, a default mirror, storage provider registries, content hashes, or cache eviction to this lifecycle. Those concerns require independent current consumers and ownership.
