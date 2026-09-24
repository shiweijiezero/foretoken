<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Data Plane

The data plane serves inference requests through OpenAI and Anthropic APIs, routes them to model replicas, and streams the generated output to clients.

Start with the repository [Quick Start](../README.md) to deploy and call a model service.

- [Frontend APIs](frontend/README.md): send Chat Completions, Responses, and Messages requests.
- [Request routing](frontend/src/router/README.md): select routing algorithms for model services.
- [Observability](../observability/README.md): inspect service metrics, alerts, and dashboards.
