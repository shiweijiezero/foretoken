# Contributing to Foretoken

English | [简体中文](CONTRIBUTING_zh.md)

We are glad you are here and appreciate your interest in improving Foretoken with us. We value contributions of every kind, including code, documentation, tests, bug reports, and design discussions. Every merged capability should have clear boundaries and be explainable, testable, and maintainable over time.

All participants are expected to follow the [Foretoken Community Code of Conduct](CODE_OF_CONDUCT.md).

## Development Principles

- State the problem before writing the solution.
- Keep each PR focused on one type of change. Do not mix refactoring, features, dependency upgrades, and formatting. Aim to keep a PR under 1,000 lines of code.
- Test functionality thoroughly. Cover critical functionality and boundaries, but do not write excessive test modules. Do not stop at proving that the code compiles, the process starts, or an object can be created.
- Experimental features must be disabled by default and provide explicit activation and failure boundaries.
- Contributors are responsible for reviewing all submitted code, including AI-assisted code, and must be able to explain its design and implementation.
- Stable contracts must not break silently. Incompatible changes require versioning, a deprecation period, and a migration path.
- Every file must begin with an open-source license notice and a concise description of its purpose.
- PRs must pass CI and formatting checks.
- Every PR must be reviewed by at least one maintainer other than its author.
- Avoid excessive defensive programming and design. Prioritize progress on the main implementation path.
- Keep implementations minimal. Add functionality with the least invasive approach and the smallest maintainable diff.
- Write comments for open-source readers to explain modules, not modification history or prior conversations.
- Add concise comments for substantial code blocks and document non-obvious algorithms and data structures. Reduce comments when the code is self-explanatory.
- Reuse mature libraries and tools whenever possible instead of rebuilding existing capabilities. This reduces maintenance cost and improves quality.
- Align with community and industry standards and interfaces, while keeping concepts decoupled from concrete implementations.
- Compare relevant frameworks and tools, evaluate the trade-offs of their designs, and make a considered choice.

## Before Writing Code

The following changes may be submitted directly as small PRs:

- documentation, spelling, and link fixes;
- focused bug fixes with clear reproduction steps;
- local cleanup that does not change external behavior;
- tests for existing behavior.

Open an issue or design proposal before implementing changes that:

- add or modify a CRD, CLI, configuration format, or public Go/Python API;
- add a controller, router, autoscaler, runtime backend, or hardware backend;
- change the protocol between the control plane and data plane;
- introduce an external dependency, testing methodology, or permanent CI job;
- reorganize components or change the deployment model;
- may affect compatibility, performance results, or resource cost.

A bug issue should include reproduction steps, expected and actual behavior, environment details, and minimal relevant logs. Do not submit only a screenshot or a statement that something "does not work."

For a major change, first open an issue whose title starts with `[Proposal]`. The design should include at least:

- the problem, context, and user scenarios;
- goals, non-goals, and affected components;
- the proposed design, interface, or data flow;
- alternatives considered and why they were rejected;
- compatibility, upgrade, and rollback plans;
- testing, observability, and success criteria;
- dependencies, CI cost, and long-term owners.

Obtain agreement from the maintainers of affected components before implementation. Approval of a proposal means the direction may be implemented; it does not exempt the resulting code from review.

## Code Boundaries

Foretoken's main directories have distinct responsibilities:

- `data-plane/`: request handling, routing, inference-engine integration, and runtime data paths;
- `control-plane/`: desired state, instance lifecycle, scaling decisions, Kubernetes resource rendering, and failure recovery;
- `benchmarks/`: correctness, randomized workloads, SLO evaluation, and simulation;
- `deploy/`: deployment composition, hardware configuration, and release artifacts.

Keep module-level unit tests next to their source. Do not use the root `tests/` directory for tests that belong to a single module.

## Opening a Pull Request

A PR description should include:

- the problem and reproduction steps;
- scope and non-goals;
- important design choices;
- validation commands and results;
- unverified areas, compatibility risks, and necessary rollback steps;
- the related issue or design proposal.

When user-visible behavior, commands, configuration, or status changes, update the relevant English and Chinese documentation, examples, and release notes. Use Draft status until the PR is ready for full review.

Keep the PR reviewable:

- do not commit secrets, tokens, server addresses, private kubeconfigs, model credentials, or local experiment data;
- do not include unrelated formatting or generated-file changes;
- do not count empty, commented-out, or initialization-only tests as coverage;
- do not accept automated suggestions without checking their correctness.

AI-assisted contributions must meet the same standards. Do not send secrets, private code, server configuration, or unpublished data to external models. Verify code provenance and licensing, and never claim to have run commands, tests, or hardware validation that did not occur. Generated explanations are not a substitute for understanding the code.

Experimental capabilities must remain disabled by default.

Before merge, a PR needs approval from at least one maintainer other than the author and must pass the required checks relevant to the change.

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

Common prefixes include `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `ci`, and `chore`. Describe the actual change instead of using vague messages such as "update" or "fix issues."

## License

Contributions to Foretoken are published under the repository's [Apache License 2.0](LICENSE). Make sure you have the right to submit all code, documentation, data, and test material included in your contribution.
