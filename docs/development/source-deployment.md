<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Source deployment lifecycle

English | [简体中文](source-deployment_zh.md)

This guide describes the behavior to preserve when maintaining source deployments. For commands and examples, see [Deploy Foretoken from Source](../deployment/source.md).

## Installation and updates

Platform installation owns shared CRDs, dependencies and runtime defaults, and prepares initial application builds. Administrators can prepare a namespace's controller, access and application defaults without reinstalling the platform. Members then submit service intent using those applications.

Source deployments build controller, frontend and model-server code from the selected checkout and update the submitted namespace. In an administrator-prepared namespace, source updates are explicit and retain their administrative permissions; ordinary service operations do not enter platform builds or cleanup. CRD changes still require platform installation.

Source updates reuse compatible runtime environments and rebuild them automatically when dependency or base-image inputs change. The CLI remembers engine directories separately for each Foretoken source directory. Engine source must remain compatible with the selected Python, PyTorch and accelerator libraries.

For example, a frontend-only update keeps unchanged model instances serving. Repeating an unchanged deployment reuses its controller and applications; configuration drift still needs to be repaired.

## Builds and publication

Go controller executables, Rust serving executables, Python adapters and inference-engine files have separate build inputs. Rebuild affected outputs, replacing each updated group in full, including deletions. Reuse unchanged files only from a completed application matching the previous inputs and runtime environment. Publish the complete application before updating services; running services must not depend on writable compiler caches.

Compiler caches survive builder replacement. Builds execute on node-local storage; completed artifacts can use a selected StorageClass. The source checkout reclaims its artifact and local execution volumes when unused. Application and model storage remain separately managed.

Independent source directories can build concurrently. Serialize each checkout's source update and each namespace's service mutations; platform installation and removal remain exclusive with source updates. Bound lock acquisition without expiring active holders. Stop and delete supersede older pending submissions. Builds and readiness observation must not hold the namespace mutation lock.

## Readiness and failure

Deployment succeeds when the selected application and its serving routes are ready. Controllers manage rollout and request draining. Start the frontend with its selected runtime cache. Service status and code-activation checks share one readiness deadline; source compilation runs separately.

Each model's `spec.timeouts.startup` supplies separate preparation and serving-startup limits. Keep a preparation failure visible through retries until preparation succeeds. Report rejected configuration as a failed target, while allowing serving-container restarts to recover within the startup limit.

## Cleanup

On failure or cancellation, stop builders and publishers before reusing compiler output, and retain compiler caches for retry. Cleanup has its own timeout so it can release locks and resources after a readiness wait expires; cleanup errors must not replace the original failure.

Preserve applications still needed by deployments, builds or rollback, and save recoverable build inputs before releasing unused resources.

`foretoken delete` waits for the submitted services and workloads to retire. An administrator-prepared namespace retains its controller and provided storage until the administrator removes them. Self-managed deployments remove an unused controller and delete a declared namespace last. Source cleanup releases unused build resources while preserving source directories, experiment results and source archives. Namespace uninstall also releases builds no longer used by its applications.

Uninstall the shared platform after its deployments have been deleted, reclaiming compiler volumes before removing their provisioner. Model data and archived inputs are retained.
