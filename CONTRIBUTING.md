# Contributing to Foretoken

English | [简体中文](CONTRIBUTING_zh.md)

Thank you for contributing code, documentation, tests, bug reports, or design discussions to Foretoken. Every merged change should have a clear boundary and remain explainable, verifiable, and maintainable.

All participants must follow the [Foretoken Community Code of Conduct](CODE_OF_CONDUCT.md). Before changing code, configuration, tests, or user documentation, read the [Foretoken Code Style](docs/development/code-style.md).

## Before You Start

The following focused changes can be submitted directly:

- documentation, spelling, and link fixes;
- bug fixes with clear reproduction steps;
- local cleanup that does not change external behavior;
- tests for existing behavior.

Open an issue or design proposal before implementing changes that:

- add or modify a CRD, CLI, configuration format, or public Go/Python API;
- add a controller, router, autoscaler, runtime backend, or hardware backend;
- change the protocol between the control plane and data plane;
- introduce an external dependency, testing methodology, or permanent CI job;
- reorganize components or change the deployment model;
- may affect compatibility, performance results, or resource cost.

A bug issue should include reproduction steps, expected and actual behavior, environment details, and minimal relevant logs.

For a major change, first open an issue whose title starts with `[Proposal]`. Include:

- the problem, context, and user scenarios;
- goals, non-goals, and affected components;
- the proposed interface or data flow;
- alternatives considered and why they were rejected;
- compatibility, upgrade, and rollback plans;
- validation, observability, and success criteria;
- dependencies, CI cost, and long-term maintenance responsibility.

Obtain agreement from the maintainers of affected components before implementation. Proposal approval confirms the direction; the resulting code still requires normal review.

## Repository Areas

- `data-plane/`: request handling, routing, inference-engine integration, and runtime data paths;
- `control-plane/`: desired state, instance lifecycle, scaling decisions, Kubernetes resources, and failure recovery;
- `benchmarks/`: correctness, workloads, performance, SLO evaluation, and simulation;
- `deploy/`: deployment composition, hardware configuration, and release artifacts.

Keep module-level unit tests next to their source. Do not use a root `tests/` directory for tests that belong to one module.

## Pull Requests

Use the repository [pull request template](.github/PULL_REQUEST_TEMPLATE.md). Keep the PR focused on one responsibility and remove unrelated formatting, generated drift, local artifacts, and obsolete paths from its final diff.

When user-visible behavior, commands, configuration, or status changes, update the relevant English and Chinese documentation and examples. Keep the PR in Draft status until it is ready for full review.

Do not commit secrets, tokens, server addresses, private kubeconfigs, model credentials, personal absolute paths, or local experiment data.

Contributors are responsible for all submitted code, including AI-assisted changes. Review every changed line, verify provenance and licensing, and report only commands and validation that actually ran. Never send private code, credentials, server configuration, or unpublished data to external models.

Before merge, a PR must pass the checks relevant to its changes and receive approval from at least one maintainer other than its author.

## Branches and Commit Messages

Use short branch names that describe their purpose, for example:

```text
feature/control-plane-baseline
fix/router-timeout
docs/contributing-guide
benchmark/slo-simulation
```

Use Conventional Commit-style messages:

```text
feat(control-plane): add inference group reconciliation
fix(router): handle unavailable backends
test(bench): cover SLO search boundaries
docs: add development guidelines
```

Common prefixes include `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `ci`, and `chore`. Describe the actual change instead of using vague messages such as `update` or `fix issues`.

## License

Contributions to Foretoken are published under the repository's [Apache License 2.0](LICENSE). Make sure you have the right to submit all code, documentation, data, and test material included in your contribution.
