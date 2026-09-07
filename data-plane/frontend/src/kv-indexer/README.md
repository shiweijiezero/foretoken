<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV Prefix Index

When several requests start with the same long system prompt, a model instance may still hold the KV cache from processing that prefix. The KV prefix index tells the Router where that prefix was observed, helping it choose an instance that can reuse the work. The cache itself stays with the inference backend; routing currently considers cache on the target's local accelerator.

With the [`kv_least_loaded` routing scorer](../router/README.md), lookup results affect selection as follows:

- **Match:** prefer longer cached prefixes, then lower load.
- **Miss:** no reusable prefix was found in the index for that target, so it receives no cache preference.
- **`Unavailable`:** the index cannot give a reliable answer. This is not a miss; the target receives no cache preference, but remains eligible for ordinary routing.

Targets must still be healthy and compatible with the request. A match makes reuse more likely, but the backend may evict the cache before execution begins.

## Operations

Use the frontend's `/statusz` endpoint to inspect KV-index health and any degradation reason, and `/metrics` for Prometheus monitoring. If the index remains degraded, use the reported reason to investigate model-server cache updates. See [frontend endpoint access](../../README.md#endpoint-access).

Protocol details and backend integration are covered in [KV index maintenance](MAINTAINER.md).
