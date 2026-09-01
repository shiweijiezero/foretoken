<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Data Plane

The data plane receives inference traffic and executes requests against the model instances selected for each model. The control plane creates its workloads and publishes their serving configuration; clients use the frontend rather than starting data-plane processes directly.

| Area | Responsibility | Does not own |
| --- | --- | --- |
| [Frontend](frontend/README.md) | OpenAI-compatible requests, streaming responses, request admission, and runtime diagnostics | DNS, TLS, ingress policy, or desired-state reconciliation |
| Router | Selects a compatible, healthy model target | Model execution or cache storage |
| KV prefix index | Supplies cache-locality observations for routing | Cache storage, cache movement, or cache-hit guarantees |
| Model server | Executes inference through the configured backend | Public ingress and service lifecycle |

Start with the repository [Quick Start](../README.md) to deploy and call a model service. Platform operators can use the [observability guide](../observability/README.md) to inspect the serving runtime. Changes to routing and KV indexing follow the maintainer notes in their component directories.
