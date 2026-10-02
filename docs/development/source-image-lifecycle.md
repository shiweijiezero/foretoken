<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Runtime source lifecycle

English | [简体中文](source-image-lifecycle_zh.md)

Source updates separate runtime code from the image that supplies its dependencies. This reference describes the ownership and activation rules for maintaining that separation. Installation and redeployment commands are in [Deploy Foretoken from Source](../custom-deployment.md).

## Preparation and publication

The CLI owns the workstation's checkout binding, saved installation settings, and input comparison. The binding identifies the cluster, installed platform, and controller-selected runtime environment; it is not shared between workstations. Source operations on the same workstation serialize by cluster. The client assigns revisions to changed paths and sends only their contents and removals; a completed input record marks the cluster workspace ready for a build.

Dedicated BuildKit Pods own compilation. Persistent compiler volumes retain the source workspace, dependency downloads, build caches, and outputs separately from model data. Existing Dockerfiles build platform images or export runtime executables. Registry builds push directly from the cluster. Local kind/k3d builds load images into the node's containerd without routing image archives through the client. An interrupted installation retains its compiler cache. After acquiring the workstation's operation lock, the next source operation retires that binding's abandoned build Pods before reusing it. Each temporary registry Secret belongs to its build Pod.

Runtime publication copies a complete component payload into a staging directory on the workload's persistent cache, then selects the revision only after publication finishes. Published directories are not modified by later updates. The publisher uses the runtime image's user and runs outside the serving Pods, so a failed inference process does not prevent preparing its replacement.

## Engine source and native extensions

An explicit engine checkout is independent of the pinned vLLM Rust dependency. Its Python source is authoritative; the runtime supplies compatible native libraries and generated or vendor files that the checkout does not contain. Foretoken's engine patches remain applied. Deleted inputs must disappear from subsequent payloads, and Python-only updates retain successful native builds.

Native builds use the selected runtime's Python, PyTorch, and accelerator environment, adding compiler tools in a separate build stage. Upstream build tools own compilation and their incremental caches. Full-image updates package the completed payload with metadata from the engine source, then use the normal package resolver to install dependencies. Accelerator ABI dependencies remain tied to the selected base image. MetaX native updates also select the compiled plugin at runtime rather than its precompiled kernel package.

## Workload activation

The CLI selects the source revision on the service; existing controllers own frontend rollout and model Pool/Group replacement. Model preparation and serving receive the same selection. At startup, the image bootstrap selects the executable, Python adapters, and engine payload. If a bundle declares an executable, a missing executable fails startup rather than silently running the image's older code.

The CLI observes the selected workloads, active source, consumed routing version, and Service endpoints before reporting deployment success. Unchanged source reuses prepared artifacts, and unchanged workload configuration does not trigger replacement.

Controllers retain admission closure, route withdrawal, request drain, and resource release for both source and image updates. Withdrawal is acknowledged by the frontend's active routing version, independently of serving readiness: a frontend with no remaining backend can acknowledge the empty routing snapshot while it is not ready to serve. Existing drain deadlines still bound unreachable consumers and unfinished requests.

## Image updates and cleanup

Changes to the image's startup code require a new image because that code runs before source activation. Dependency, build, control-plane, and Helm changes also use the platform installation lifecycle. When runtime storage is unavailable, or a single-node writable claim is awaiting its first placement, deployment uses images instead; model preparation retains ownership of initial storage placement.

Image reuse compares build output with installed references and the requested distribution destination. A successful source installation clears service source selections so workloads use the newly built images. Local snapshots are retired when no longer referenced. Runtime payload cleanup preserves service intent, retained rollout templates, and running or terminating consumers across namespaces that may share a data directory. It removes only the current binding's unreferenced publications, leaving other writers' candidates intact.

Compiler volumes used for runtime updates follow the model cache's lifecycle. Source uninstall removes managed compiler caches and the workstation binding without deleting model data. vLLM-Omni retains its separate [image build recipe](../custom-deployment.md#vllm-omni-runtime).
