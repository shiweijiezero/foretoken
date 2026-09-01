<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Code Style

English | [简体中文](code-style_zh.md)

This guide defines the design, implementation, testing, configuration, and documentation standards for Foretoken. It complements [CONTRIBUTING.md](../../CONTRIBUTING.md), which describes the contribution process.

## Scope and authority

These rules apply to first-party Foretoken code and release artifacts. Vendored or upstream-managed sources follow their own project conventions unless a Foretoken adapter is being changed. New first-party source, configuration, and script files must use the repository's SPDX license notice.

Executable configuration is authoritative for mechanical formatting and generated files. Use the repository Makefiles, Cargo configuration, Go tools, package configuration, and pre-commit hooks rather than duplicating their options in prose. This guide defines the ownership and maintainability rules that formatters cannot enforce.

## Design and ownership

Use the smallest maintainable implementation that completely expresses the required behavior and failure semantics.

- Every type, helper, configuration field, validation, fallback, and test must have a current responsibility. Remove it when its consumer is removed.
- Prefer deleting duplicate paths, reusing an existing owner, or calling a mature upstream API before adding another abstraction.
- Introduce a trait, interface, factory, builder, or registry only when it represents a real ownership boundary or multiple current implementations.
- Keep one owner for each lifecycle. Resource creation, use, cleanup, and state publication should stay together unless ownership transfer is explicit in the API.
- Do not copy a complete lifecycle into multiple runners, controllers, or adapters. Share the common execution path and keep each caller responsible only for its distinct policy or input.
- Shared protocol values, defaults, state transitions, and derived calculations must have one authoritative definition. Consumers should use a typed contract or shared helper instead of copying strings, numbers, or algorithms.

Foretoken integrates with mature inference engines and Kubernetes projects. Reuse their public interfaces and owning paths where they fit. Keep version-sensitive or backend-specific behavior in a thin adapter; platform APIs and common control flow should use inference-engine-neutral domain types.

Do not build compatibility probes, fallback implementations, or plugin frameworks for versions and backends the repository does not currently support.

## Public interfaces and configuration

Public configuration includes CLI flags, YAML fields, environment variables, CRD fields, API fields, status fields, and persistent output schemas. Each addition must represent a choice users currently need to make and must have an execution path that consumes it.

- Do not expose internal revisions, temporary paths, controller-owned state, runtime identities, intermediate results, or orchestration context as ordinary user configuration.
- Derive values from the model identifier, Kubernetes resource, Helm release, namespace, or another authoritative input when possible.
- Do not automatically expose every struct or dataclass field through reflection. CLI, configuration, and sweep surfaces require an explicit field mapping.
- Keep one source for each default. Do not repeat the same default in a schema, parser, controller, adapter, and example.
- Experimental capabilities must be disabled by default and have an explicit activation boundary.
- Stable contracts must not break silently. Incompatible changes require an explicit version, deprecation period, and migration path.

YAML should be easy to read and modify:

- Minimize the number of required fields and keep the common path short.
- Use specific domain names that explain what a field controls. Avoid broad names such as `target`, `policy`, `owner`, `extra`, or `config` when a precise name exists.
- Put advanced choices in a clearly owned nested block instead of mixing them into the common path.
- Do not provide two fields or files for the same decision.
- Keep `values.yaml`, its schema, maintained examples, and user documentation synchronized.
- Show the smallest useful configuration in user examples, not every optional field.

## Errors, limits, and guardrails

Do not add validation, fixed limits, retries, fallbacks, hashes, baselines, or process gates by default. Prefer a direct implementation, clear ownership, types, versions, primary keys, transactions, unique constraints, and ordinary tests. Add a defensive mechanism only when those simpler tools cannot protect a currently reachable and important failure.

When such a mechanism is necessary, identify:

1. the input or runtime state that can reach the failure;
2. the component that owns the affected resource or contract;
3. the observable signal and user-visible result;
4. why types, versions, primary keys, transactions, upstream validation, or ordinary tests are insufficient.

Validate once at the boundary that owns the invariant. Do not repeat the same check in every downstream layer. Required state and protocol failures should surface clearly rather than being converted to empty results or silent defaults.

Catch only errors the caller can handle. Broad exception handling must not turn programming errors, invalid SDK usage, or broken invariants into ordinary remote failures.

## Naming, structure, and comments

Names should describe domain responsibility and ownership without relying on the current discussion. Prefer `create_client`, `metrics_url`, or `model_group` over vague verbs and generic nouns.

Keep values at the narrowest scope that owns them. A value used by one function belongs inside that function; use a module-level constant only when multiple functions share it or it represents a module-wide external contract. Do not move configuration into file globals merely for convenience.

Keep related stages readable as one flow. Extract a function when it owns a meaningful stage or lifecycle, not merely to shorten another function. Avoid fragmenting sequential logic into thin helpers that force readers to jump between files.

Comments explain information the code cannot express clearly:

- why stages must run in a particular order;
- who owns a resource or state transition;
- which external contract constrains the implementation;
- what remains true after failure or cleanup.

Every first-party public production function must have a concise doc comment stating what it does, who calls or consumes it, what it returns or publishes, and any ownership or lifecycle responsibility that matters. Private functions that own a complete stage or are not self-explanatory need the same context. Generated code and third-party sources follow their owning project.

Large code blocks, multi-stage flows, non-obvious algorithms, and critical boundaries must have a small number of structural comments that let maintainers understand the execution before reading details.

Do not comment getters, field forwarding, constant mappings, obvious loops, or each line of a function. Do not record modification history, review replies, temporary plans, or the implementer's reasoning process in code comments.

## Language and repository tools

Follow the surrounding code and the executable checks for each subtree.

- **Rust data plane:** run `make verify-data-plane`. It checks formatting, compilation, tests, and Clippy for first-party workspace packages.
- **Go control plane:** run `make -C control-plane verify`. Generated code and CRDs must be regenerated through the Makefile targets rather than edited independently.
- **Python CLI:** keep public argument definitions and parsing separate from command execution, return explicit command types rather than passing `argparse.Namespace` across that boundary, and exercise the affected installed command against Kubernetes.
- **Python benchmarks:** follow the package structure and typing style already used under `benchmarks/`; run the relevant benchmark tests and exercise the affected benchmark command or runner path.
- **Helm and Kubernetes YAML:** run Helm lint and render the changed modes. Verify generated resources, values schema, selectors, ports, namespaces, and network access as one deployment contract.
- **Repository-wide files:** run `pre-commit run --all-files` for file hygiene, structured formats, merge conflicts, secret detection, and GitHub Actions linting.

Generated files must identify or have a documented source. Change the source and regenerate the artifact; do not maintain generated output as a second implementation.

## Testing

Do not add tests by default. Before writing test code, a human contributor must review its motivation and identify the important behavior or concrete regression it protects, why existing integration or end-to-end validation is insufficient, and why the maintenance cost is justified. If those questions have no clear answer, do not add the test.

Test code is maintained code and can expand quickly. Use the smallest set that protects behavior maintainers and users actually rely on.

- Prefer end-to-end, integration, and cross-module contract tests that cover substantial real behavior.
- Give every retained non-trivial test a descriptive name and a short preceding comment that states the protected contract, concrete failure, or reason the test exists. Document the structure of large fixtures and multi-stage scenarios.
- Extend an existing scenario, fixture, or contract test before creating a new test module.
- Add focused tests for important algorithms, concurrency, state transitions, recovery paths, or regressions that have occurred and can recur.
- Test stable observable outcomes such as API responses, CRD status, protocol events, metrics, resource lifecycle, and generated deployment behavior.
- Do not test plain getters, field forwarding, constant mappings, branch-free thin wrappers, framework wiring, or behavior already owned by an upstream library.
- Do not add a test merely because a function, branch, loader, state machine, or public method was added. Complexity and coverage numbers are not sufficient reasons.
- Do not expand a public API only to make private implementation details testable.

When a change affects model quality, serving performance, GPU behavior, or distributed deployment, code tests do not replace the relevant measurement or end-to-end execution.

## Continuous integration

Do not add permanent CI jobs, workflows, matrices, or merge gates by default. Before adding CI, a human contributor must review its motivation and identify the currently reachable failure it protects, why existing checks or direct execution cannot expose that failure, and who will maintain its runtime cost and failures.

CI configuration also expands quickly and duplicates setup, build, and deployment lifecycles easily. Prefer extending an existing job, reusing the repository's owning command, or running a focused check within the relevant workflow. Do not turn a temporary incident, one-time migration, or hypothetical future regression into permanent CI. If necessity and long-term ownership are unclear, do not add the check.

New CI must report actionable failures, remain proportionate to the change risk, and avoid repeating behavior already protected by another job or upstream project.

## Documentation

Write documentation for the person who will use or maintain the final system, not for the current PR discussion.

Before adding or substantially rewriting user documentation:

1. verify the current code, CLI help, Helm values, schema, and maintained examples;
2. read the latest documentation from at least two mature projects with a similar responsibility;
3. compare their reader path, terminology, examples, prerequisites, and troubleshooting depth before choosing the Foretoken structure.

Learn from the organization and ownership of those documents; do not combine or copy their text. Deprecated, historical, draft, and proposal documents may provide background but are not templates for current user guidance.

User documentation should begin with purpose and a minimal executable example. Move exhaustive configuration, architecture, and troubleshooting details behind the main path. Describe only implemented behavior and necessary limits. Do not publish PR plans, acceptance criteria, implementation diaries, private wire contracts, source-file tours, or future capabilities as current behavior.

When adding a capability, do not mechanically append another paragraph to the existing documentation. Reconsider the reader path and section structure, state shared behavior once, and use a table or focused subsections for real differences. Remove replaced, repeated, or no-longer-actionable content so feature growth does not become documentation growth by accumulation.

Introduce each command block with a sentence that states what the reader is about to do and, when relevant, what the command produces or where to continue. A heading alone is not sufficient context. Present mutually exclusive alternatives, such as pip and uv, with separate prose labels and code blocks instead of `# or` comments inside one block. Keep commands in one block only when readers should run them in sequence.

Keep English and Chinese documentation aligned in capability, prerequisites, commands, defaults, and limitations. Update both when user-visible behavior changes.

Write every language version independently for its readers rather than using another language as a sentence-by-sentence template. English documentation should use clear, natural technical English; Chinese documentation should use natural Chinese prose and established Chinese technical terms. Preserve exact commands, field names, type names, and algorithm identifiers when readers need to recognize or enter them, and explain uncommon identifiers in the surrounding language on first use. Avoid untranslated ordinary words when that language has a clear expression. Review each language version on its own for fluency; equivalent capability does not require identical sentence structure.

## Change and PR discipline

Keep each PR focused on one responsibility and make its final diff the smallest maintainable expression of that change.

- Do not mix a feature with unrelated refactoring, dependency upgrades, generated drift, or repository-wide formatting.
- Remove replaced implementations, unused configuration, duplicate defaults, obsolete documentation, and tests that only protected the old path.
- Verify the PR base before review. A stacked PR should identify its predecessor and must not reintroduce files or behavior already replaced on `main`.
- Do not commit credentials, private infrastructure details, personal absolute paths, local caches, temporary outputs, or experiment artifacts.
- Report the commands actually run and their results. State important unverified areas directly; do not claim CI, hardware, Kubernetes, or performance validation that did not occur.

Use [.github/PULL_REQUEST_TEMPLATE.md](../../.github/PULL_REQUEST_TEMPLATE.md) to present the problem, changes, validation evidence, performance, impact, and related issue or proposal without copying this guide into the PR body.
