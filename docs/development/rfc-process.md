<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# RFC Process

English | [简体中文](rfc-process_zh.md)

Use an RFC to discuss a new cross-component direction or platform capability before implementation. An RFC is a GitHub Issue, not a replacement for the implementation PR.

## When to open an RFC

Open an RFC before changes that introduce a new platform capability, cross-component architecture, public lifecycle, or protocol. Focused bug fixes, local cleanup, and documentation changes do not need an RFC unless they expose a broader design decision.

## Create the RFC

Create a GitHub Issue with the title format:

```text
RFC: <short title>
```

Include:

- motivation and user scenarios;
- goals and non-goals;
- affected components and ownership;
- proposed flow, interfaces, or lifecycle;
- alternatives and rejected options;
- compatibility, rollout, and rollback;
- validation, observability, and success criteria;
- dependencies and long-term maintenance responsibility.

## Discuss and implement

Discuss the design in the Issue with the maintainers of affected components. Keep the Issue focused on the proposed direction and update it when the design changes. Once the direction is accepted, open focused implementation PRs that link the RFC and explain the implemented scope.

RFC approval confirms the direction; it does not approve the code. Implementation PRs still require normal review, validation, generated-artifact checks, and documentation updates.

## Example

The proposed Agent-native Inference Infrastructure should begin as a GitHub RFC describing the overall architecture. The initial `agent-playbooks/` directory can then be introduced by a focused PR linked to that RFC.
