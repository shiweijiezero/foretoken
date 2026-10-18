<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Runtime cache lifecycle

English | [简体中文](runtime-cache_zh.md)

For deployment configuration, see [Model storage](../features/model-storage.md).

## Storage ownership

RuntimeCache selects a namespace-local claim and publishes its readiness. In an administrator-prepared namespace, a directory must match an authorized directory binding; the RuntimeCache borrows that claim without adopting or deleting it. An explicit directory takes precedence over the administrator's existing-claim default. Members do not need access to PVs or other namespaces.

For self-managed directory deployments, the CLI resolves node-visible paths and supplies the static PV after the controller creates its PVC. Local k3d placement follows the actual host bind mounts. Multi-node directory storage must expose the same filesystem to its consumers.

Dynamic caches request storage from a StorageClass. If `maxSize` is configured, the controller requests expansion when the lowest observed free-space ratio reaches 20%, doubling capacity up to the maximum. Workloads can continue using a bound volume when an expansion request fails.

## Model references and collection

A service using model X keeps a source reference even while stopped. Deleting the service first retires its preparation and serving workloads, then releases that reference. Another service using X keeps X available; a service using only Y does not retain X.

The Pool retains its selected source record independently of its current replica count. Old execution cohorts retain their own records through draining. Provider acquisition publishes fixed model and tokenizer versions before accessing shared files, so a moving branch does not change an existing reference after a restart.

Preparation and collection coordinate through one stable filesystem lock per model-cache root. The root's persistent identity distinguishes independent directories while recognizing namespace claims and mount aliases for the same data. Collection rechecks live source references under the exclusive lock before deleting provider-owned snapshots and unreferenced blobs. Local model files are outside this managed area.

The platform owns collection workers and their temporary storage bindings; namespace controllers own service intent and source-record retirement. Native NFS, NFS CSI and directory-backed hostPath volumes can be mounted through dedicated Retain aliases without changing the original volume. Other dynamically provisioned storage follows its storage driver's lifecycle. Worker completion precedes cleanup of its Job and aliases, and final release of the source record.

## Cache layout and application updates

Current applications use `shared/models` for provider downloads, `namespaces/<namespace>` for mutable runtime state, and `models` for user-maintained local files. Hugging Face account state stays separate from the shared Hub file cache. Model repository configuration and tokenizer files belong with the shared model artifacts, not with runtime configuration.

An application selection pins its preparation contract along with its executable. Older applications retain their earlier input and cache layout until an explicit deployment selects the current application. Updating a controller must not send a new preparation protocol to a retained older image.

Triton uses the namespace's `triton/<node-name>` directory to separate node writers while retaining reuse after Pod replacement. Engine-managed compiler subdirectories remain under the inference engine's control. For single-node services, model-server can stop a failed EngineCore and retry with Pod-local temporary storage when the persistent cache fails its write probe. Readable persistent model files can still be reused. Multi-node members restart together.

Profiling publishes to `namespaces/<namespace>/profiles/runs`. Each capture plan retains its actual output directory, including the earlier layout used by older applications. The viewer reads namespace-local results and explicitly retained historical run paths; profiling is unavailable during a temporary-cache retry.

## Volume retention

Retaining a RuntimeCache removes its PVC ownership so a later compatible cache can adopt the claim. Namespace deletion still removes namespaced PVCs. Directory PVs use Retain: deleting their Kubernetes objects does not recursively delete user files or namespace-local state. Managed model downloads have the separate reference lifecycle above.

With `retentionPolicy: Delete`, the RuntimeCache controller waits for workloads to release the PVC before completing deletion, and the CLI removes its directory PV object. Dynamic volumes follow their StorageClass reclaim policy. Rebinding a retained directory volume uses the observed claim UID and resource version to avoid replacing a concurrent binding.
