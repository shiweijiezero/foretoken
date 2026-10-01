<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Runtime source lifecycle

English | [简体中文](source-image-lifecycle_zh.md)

Source updates separate runtime code from the image that supplies its dependencies. This reference describes the ownership and activation rules for maintaining that separation. Installation and redeployment commands are in [Deploy Foretoken from Source](../custom-deployment.md).

## Preparation and publication

The CLI owns the workstation's checkout binding, saved installation settings, input comparison, and artifact preparation. The binding identifies the cluster and installed platform; it is not shared between workstations. Source operations on the same workstation serialize by cluster so preparation and cleanup cannot remove artifacts another operation is using.

For the Foretoken frontend and vLLM model-server, Rust changes export executables through the existing Docker builder and compilation cache. Python adapter changes replace the complete adapter directory. A Python-only update retains the component's last compiled executable. Input snapshots are committed only after checking that the checkout did not change during preparation.

A bundle is the complete executable and adapter payload for one component revision. The CLI uploads it to a staging directory in persistent runtime storage and publishes it before selecting that revision on a service. Published directories are immutable. A separate CPU publisher allows publication even when the previous inference process cannot start.

## Workload activation

The CLI selects the source revision on the service; existing controllers own frontend rollout and model Pool/Group replacement. Model preparation and serving must receive the same selection. At startup, the image bootstrap selects the executable and Python adapters. If a bundle declares an executable, a missing executable fails startup rather than silently running the image's older code.

The CLI checks the selected serving workloads and their active source before reporting deployment success. An earlier Ready workload does not prove that the update is active. Unchanged source reuses prepared artifacts, and unchanged workload configuration does not trigger replacement.

## Image updates

Bootstrap changes require a new image because the bootstrap runs before source activation. Dependency, build, control-plane, and Helm changes also use the existing platform installation lifecycle. When runtime storage is unavailable, or a single-node writable claim is awaiting its first placement, deployment uses images instead; model preparation retains ownership of initial storage placement.

Image preparation compares the build with deployed image references and the requested distribution destination. A successful source installation clears existing service source selections so workloads use the newly built images. Local snapshots and bundles are retired when no longer referenced by the saved binding; uninstall removes the workstation binding without deleting remote model data.

vLLM Python/CUDA sources and vLLM-Omni builds remain part of their inference-engine image paths. They are not runtime adapter bundles; their user-facing entry points are in [Use a custom inference engine](../custom-deployment.md#7-use-a-custom-inference-engine).
