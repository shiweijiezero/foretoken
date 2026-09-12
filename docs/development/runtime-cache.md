<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Runtime cache lifecycle

English | [简体中文](runtime-cache_zh.md)

For deployment configuration, see [Model storage](../model-storage.md).

## Storage ownership

A namespace's RuntimeCache owns its PVC and publishes the claim name and readiness. Workload controllers mount that claim into model-server and frontend Pods. An administrator-supplied `workload.cache.claimName` takes precedence and is not managed by the RuntimeCache controller.

Dynamic caches request storage from a StorageClass. If `maxSize` is configured, the controller requests expansion when the lowest observed free-space ratio reaches 20%, doubling capacity up to the maximum. Workloads can continue using a bound volume when an expansion request fails.

For directory caches, the CLI resolves node-visible paths before applying user intent. The RuntimeCache controller creates the PVC and determines its name, volume name, access mode and binding capacity. The CLI reads that claim to prepare a static PV; it does not maintain a second naming or capacity algorithm. Binding capacity does not create a filesystem quota.

Local k3d mounts and single-node directories receive PV node affinity. A multi-node absolute path declares a filesystem already shared at that location on every node. This mode does not provision shared storage or transfer files between nodes.

## Retention and redeployment

The controller removes PVC ownership when retaining a deleted cache. A new directory cache can adopt the unchanged retained claim only when its path, volume binding and ownership match. Namespace deletion still removes namespaced PVCs.

The CLI retains directory PVs by default. When a namespace is recreated, it can rebind the same PV to the controller's new PVC after the old claim is gone. Resource-version and old-claim preconditions prevent overwriting concurrent binding changes. Existing PVs owned by another deployment, or using another path or node placement, are not adopted.

With `retentionPolicy: Delete`, the controller deletes the PVC after its workloads release it, and the CLI deletes its directory PV object. The volume reclaim policy remains Retain so files are not removed. Dynamic volumes follow their StorageClass reclaim policy.

## Model files and runtime caches

The model-files library owns local model/tokenizer directory resolution for both data-plane consumers. The platform projects the model root below the data mount, separately from provider and compilation cache locations. The shared resolver keeps relative references inside that model root and rejects individual files rather than replacing them with parent directories. It does not download artifacts or validate model formats; those responsibilities remain with the inference engine and tokenizer loaders. Public model identifiers remain unchanged after resolving local files.

The runtime cache mount stores model-provider caches and engine compilation caches. Model-server owns its startup write probe and one temporary-cache retry: it stops the failed EngineCore before retrying under the Pod's `/tmp` volume. A frontend with a missing Hub snapshot can use its temporary tokenizer cache. This fallback neither changes a running engine's storage path nor copies prepared local checkpoints.

The persistent data root contains `models`, `vllm`, `torch`, and `triton`. Prepared models resolve under `models`; Hub repositories retain the upstream `models/hub` layout with revision-aware cache lookup. Temporary startup retries relocate only projected provider/compiler cache paths, while prepared local models continue resolving from the persistent model root. No namespace or Pod identity is added to persistent model paths.
