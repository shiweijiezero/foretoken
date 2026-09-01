<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV Index Maintenance

English | [中文](MAINTAINER_zh.md)

The KV index consumes backend-adapter events and produces locality observations for Router scoring. It does not own cache storage or cache movement.

## Protocol boundary

Each event source is isolated by model revision, KV scope, route target, and data-parallel rank. Event streams use epochs and continuous cursors. Missing, reordered, or incompatible events must degrade the source to `Unavailable` rather than expose a false locality match.

The generic protocol can represent `Device`, `HostPinned`, `Disk`, and `External` placements. Current Foretoken serving projection enables only local `Device` placement and disables cache restore or transfer. Do not document a generic enum as a product capability before the Controller, runtime, transport, diagnostics, and rollout path support it.

## Index resolution

`PositionalHashIndex` and `RadixTreeIndex` are internal implementations selected by topology-aware configuration. `NoopKvPrefixIndexer` is a Router no-op used when locality is unavailable. None is a user-selectable `FrontendService` setting.

## Backend adapters

Backend-specific block identifiers, event lifecycles, and placement semantics belong in a backend adapter. Preserve exact event-source identity and sequence rules when extending an adapter. A backend must publish enough fidelity for the index to distinguish confirmed matches, confirmed misses, and unavailable observations.

When changing the protocol or index behavior, update the user-facing KV guide, Router guide, runtime diagnostics, metrics, and contract tests together.
