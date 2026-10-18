<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Source deployment lifecycle

English | [简体中文](source-deployment_zh.md)

This guide describes the behavior to preserve when maintaining source deployments. For commands and examples, see [Deploy Foretoken from Source](../deployment/source.md).

## Installation and updates

Platform installation owns shared CRDs, dependencies and runtime defaults, and prepares initial application builds. Deployment builds controller, frontend and model-server code from the selected checkout and updates the submitted namespace. Each namespace has a controller that reconciles only its resources, so controller code changes use the same deployment path as serving code. CRD changes still require platform installation.

Source updates reuse compatible runtime environments and rebuild them automatically when dependency or base-image inputs change. The CLI remembers engine directories separately for each Foretoken source directory. Engine source must remain compatible with the selected Python, PyTorch and accelerator libraries.

For example, a frontend-only update keeps unchanged model instances serving. Repeating an unchanged deployment reuses its controller and applications; configuration drift still needs to be repaired.

## Builds and publication

Go controller executables, Rust serving executables, Python adapters and inference-engine files have separate build inputs. Rebuild affected outputs, replacing each updated group in full, including deletions. Reuse unchanged files only from a completed application matching the previous inputs and runtime environment. Publish the complete application before updating services; running services must not depend on writable compiler caches.

Compiler caches persist across builder replacement. An explicitly selected build StorageClass holds completed artifacts, while execution state stays on automatically managed local storage. Publication reads only completed exports; source archives are read from the corresponding local workspace. Source-deployment cleanup reclaims both the artifact and local execution volumes when they are no longer in use. Application and model storage retain their own configuration and lifecycle.

Independent source directories can build concurrently. Serialize source preparation and submission within one source directory, and keep platform installation and removal exclusive. Bound the wait to acquire a lock without expiring an operation that already holds it. Waiting for service readiness must not prevent deletion from releasing occupied devices.

## Readiness and failure

Deployment succeeds when the selected application and its serving routes are ready. Controllers manage rollout and request draining. Start the frontend with its selected runtime cache. Service status and code-activation checks share one readiness deadline; source compilation runs separately.

Each model's `spec.timeouts.startup` supplies separate preparation and serving-startup limits. Keep a preparation failure visible through retries until preparation succeeds. Report rejected configuration as a failed target, while allowing serving-container restarts to recover within the startup limit.

## Cleanup

On failure or cancellation, stop builders and publishers before reusing compiler output, and retain compiler caches for retry. Cleanup has its own timeout so it can release locks and resources after a readiness wait expires; cleanup errors must not replace the original failure.

Preserve applications still needed by deployments, builds or rollback, and save recoverable build inputs before releasing unused resources.

`foretoken delete` waits for service and workload cleanup before removing an unused namespace controller. A namespace included in the submitted configuration is deleted last. It also cleans up build resources no longer in use, preserving source directories, experiment results and source archives.

Uninstall the shared platform after its deployments have been deleted, reclaiming compiler volumes before removing their provisioner. Model data and archived inputs are retained.
