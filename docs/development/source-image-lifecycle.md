<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Source update lifecycle

English | [简体中文](source-image-lifecycle_zh.md)

Installation and update commands are in [Deploy Foretoken from Source](../custom-deployment.md). The following constraints apply when maintaining the update path.

## Build and publish

Source operations from one workstation are serialized per cluster. Synchronization includes file deletions; engine updates must not restore deleted source from the runtime image. Compilation uses caches separate from model data and preserves compatibility with the selected runtime's Python and accelerator libraries.

A complete application version is published before use and remains immutable. Stop a build's abandoned publishers before reusing its output. The platform owns application storage independently of compiler and model caches.

## Select and recover

Controllers save the selected runtime and application version before creating workloads. Model preparation and serving use the same version; restarts and scaling retain it. Platform installation updates defaults, while explicit redeployment updates existing services.

Deployment success requires the selected version and serving routes to be ready. Existing controllers own rollout and request draining.

## Clean up

Keep versions referenced by services, workload templates, running or terminating workloads, saved tasks and Helm rollback history. Remove only the publisher's unreferenced versions, leaving other publishers' work intact.

Helm owns application storage. Source uninstall removes managed compiler caches and the workstation binding while preserving model data.
