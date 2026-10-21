<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Source deployment lifecycle

English | [简体中文](source-deployment_zh.md)

This guide describes the ownership and lifecycle maintained by source deployments. Installation, deployment and parallel experiment commands are in [Deploy Foretoken from Source](../deployment/source.md).

## Installation and updates

Platform installation owns shared CRDs, dependencies, runtime defaults and the deployment manager. A source installation also prepares initial applications and publishes the build settings needed by developer workstations. Users receive namespace access through administrator-managed RoleBindings; installation does not grant user permissions.

The CLI submits an `InferenceDeployment` for each named instance. The platform manager selects its controller from the installed template and prepares controller permissions, authorized directory bindings and Gateway access. Each controller reconciles only its instance, including when neighboring instances use different controller code in the same namespace. An older namespace-wide controller must retire before instance controllers can take ownership there.

Source development is explicit through `deploy -e` or an engine-source selection. It builds the selected checkout's controller, frontend and model-server applications and updates only the target instance. Ordinary deployment uses installed applications without source builds. CRD and shared platform changes still require platform installation.

A new workstation adopts the platform's completed build and captured inputs rather than reinstalling it. Source discovery uses directory contents and declared dependency versions; Git supplies optional provenance. Source updates reuse compatible runtime environments and rebuild when dependencies or base-image inputs change. Each source directory retains its own engine selection, which must match the selected Python, PyTorch and device environment.

A frontend-only update keeps unchanged model instances serving. Repeating an unchanged deployment reuses its controller and applications; configuration drift still needs to be repaired. Later platform defaults are selected on an explicit deployment, not by restarting an existing instance.

## Builds and publication

Go controller executables, Rust serving executables, Python adapters and inference-engine files have separate build inputs. Rebuild affected outputs, replacing each updated group in full, including deletions. Reuse unchanged files only from a completed application matching the previous inputs and runtime environment. Publish the complete application before updating services; running services must not depend on writable compiler caches.

BuildKit prepares reusable environment and compiler images. Independent compiler Pods share node-local dependency downloads, the Go build cache and ccache; each source directory keeps private Rust target and CMake trees. Compiler state survives Pod replacement. Completed artifacts can use a separate StorageClass, and application and model storage retain their own lifecycles.

Independent checkouts can build concurrently. Serialize source updates within one checkout and service mutations for one deployment instance. Source operations sharing a workstation state directory exclude platform changes during builds. The deployment manager rejects new instances while the platform retires. Bound lock acquisition without expiring active holders. Stop and delete supersede older pending submissions; builds and readiness observation do not hold an instance's mutation lock.

## Readiness and failure

Deployment succeeds when the selected application and its serving routes are ready. Controllers manage rollout and request draining. Start the frontend with its selected runtime cache. Service status and code-activation checks share one readiness deadline; source compilation runs separately.

Each model's `spec.timeouts.startup` supplies separate preparation and serving-startup limits. Keep a preparation failure visible through retries until preparation succeeds. Report rejected configuration as a failed target, while allowing serving-container restarts to recover within the startup limit.

## Cleanup

On failure or cancellation, stop builders and publishers before reusing compiler output, and retain compiler caches for retry. Cleanup has its own timeout so it can release locks and resources after a readiness wait expires; cleanup errors must not replace the original failure.

Preserve applications still needed by deployments, builds or rollback, and save recoverable build inputs before releasing unused resources.

`foretoken delete` waits for the selected instance's services, workloads and model references to retire before the platform removes its controller and instance resources. Other instances, the Kubernetes namespace and administrator-provided storage remain. Dynamic caches follow their retention policy. Development environments, captured inputs and incremental build state have an independent lifetime and remain available for subsequent source deployments.

Uninstall the shared platform only after its deployment instances are gone. Reclaim the platform's managed development volumes before removing their provisioner. Model-download collection follows the separate [runtime cache lifecycle](runtime-cache.md).
