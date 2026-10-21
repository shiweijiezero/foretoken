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

Start with the user's intended outcome and the interface they can most easily understand and operate. Minimize the concepts they must learn, information they must supply, and manual steps they must perform. Keep genuine choices and external prerequisites explicit; let the owning component derive values and prepare or coordinate resources it already controls.

Design the complete configuration, execution, observation, decision, and cleanup path around that interface before changing one stage. Keep shared identities, state scopes, and lifecycle ownership consistent across producers and consumers. When the existing structure forces repeated special cases, duplicate decisions, or manual coordination, restructure, merge, or replace the path at its owning boundary and migrate affected consumers together. Prefer a coherent system design over the smallest diff; use a local correction when the existing boundary is sound. Keep the work focused on the current outcome, preserve required behavior and failure semantics, and remove superseded paths.

- Every type, helper, configuration field, validation, fallback, and test must have a current responsibility. Remove it when its consumer is removed.
- Prefer deleting duplicate paths, reusing an existing owner, or calling a mature upstream API before adding another abstraction.
- Introduce a trait, interface, factory, builder, or registry only when it represents a real ownership boundary or multiple current implementations.
- Keep one owner for each lifecycle. Resource creation, use, cleanup, and state publication should stay together unless ownership transfer is explicit in the API.
- Do not copy a complete lifecycle into multiple runners, controllers, or adapters. Share the common execution path and keep each caller responsible only for its distinct policy or input.
- Shared protocol values, defaults, state transitions, and derived calculations must have one authoritative definition. Consumers should use a typed contract or shared helper instead of copying strings, numbers, or algorithms.

## Community libraries and frameworks

Before adding or substantially revising a general-purpose responsibility, actively look for maintained community libraries, SDKs, tools, and frameworks, beyond the standard library and dependencies already in the repository. This includes parsing and validation, protocol clients, concurrency, persistence, scheduling, and observability, as well as inference engines and Kubernetes integrations.

Evaluate candidates through their actual public APIs, documentation, and relevant source. Check maintenance, licensing, supported versions, dependency and deployment costs, and the required behavior under concurrency, cancellation, deadlines, failure, and recovery. Prefer the option that reduces total implementation and maintenance work while preserving the component's responsibilities and supported capabilities.

Reuse the upstream component's complete owning path rather than wrapping its types around another handwritten implementation. Keep only necessary domain adaptation, migrate producers and consumers together, and delete superseded code, state, configuration, and tests. Common APIs remain engine-neutral; backend- or version-specific details stay in thin adapters. When domain code must remain, explain the concrete requirement the available upstream interfaces do not cover in the relevant design or review, rather than relying on a generic claim that custom code is simpler.

Do not build compatibility probes, fallback implementations, or plugin frameworks for versions and backends the repository does not currently support.

## Public interfaces and configuration

Public configuration includes CLI flags, YAML fields, environment variables, CRD fields, API fields, status fields, and persistent output schemas. Each addition must represent a choice users currently need to make and must have an execution path that consumes it.

- Do not expose internal revisions, temporary paths, controller-owned state, runtime identities, intermediate results, or orchestration context as ordinary user configuration.
- Derive values from the model identifier, Kubernetes resource, Helm release, namespace, or another authoritative input when possible.
- Do not automatically expose every struct or dataclass field through reflection. CLI, configuration, and sweep surfaces require an explicit field mapping.
- Keep one source for each default. Do not repeat the same default in a schema, parser, controller, adapter, and example.
- Define user and runtime defaults at the configuration or lifecycle boundary that owns them. Decoders and consumers must not replace missing required state with empty strings, empty collections, zero values, or other convenient fallbacks; fail at the owning boundary so the broken contract remains visible. Represent absence explicitly only when it is valid domain state.
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

Group module-level imports, shared constants, and type declarations near the top in dependency order rather than interleaving them with function implementations. Keep core functions clear and concise, and place the main entry points after these declarations so readers encounter the primary flow before supporting details; use the same ordering within classes.

Keep related stages readable as one flow. Extract a function when it owns a meaningful stage or lifecycle, not merely to shorten another function. Avoid fragmenting sequential logic into thin helpers that force readers to jump between files.

When a component owns the same resources, clients, configuration, or invariants across multiple lifecycle stages, use a state-owning type: a class in Python or a struct with methods in Go and Rust. Keep stateless transformations and single-stage operations as functions. Do not introduce a type only for namespacing, shorter parameter lists, or hypothetical reuse.

A module centered on a state-owning class should contain that lifecycle and its closely related types. Put behavior shared by current implementations on their base class, and keep adapter-specific recognition, capacity, and policy as methods on the specialized class so the adapter remains readable in one place. Move only reusable stateless parsing, matching, and formatting to clearly named helper modules. Do not enforce one class per file mechanically; keep multiple classes together when they participate in the same lifecycle and share one responsibility.

Keep directory hierarchies understandable without opening every file. When a directory accumulates files for several responsibilities, group them by their existing domains or lifecycles so readers can find the entry point and related implementation together. Group engine, hardware, and provider implementations under the domain they implement rather than making each one a peer of unrelated core subsystems. Each level must explain a responsibility; do not create empty hierarchies, generic `utils` layers, registries, or directories for hypothetical implementations.

Review every first-party source file exceeding 300 effective code lines and every function exceeding 150. Count lines containing code, excluding blank lines, comment-only lines, and documentation strings; a line containing both code and a comment counts. Examine responsibility boundaries, abstraction levels, state ownership, and readability, and decide whether to retain, reorganize, or refactor each definition. These are review thresholds, not mandatory splitting limits: cohesive declarations, mappings, and algorithms may remain together.

Split a module when it accumulates independent lifecycles, protocols, backends, or ownership boundaries. Keep primary functions focused on meaningful stages of the complete operation; move domain policy, protocol handling, and resource construction into the stages that own them. Preserve a readable execution order and shared cleanup rather than shortening the entry point through chains of forwarding helpers.

After substantial feature growth, review the final responsibility map rather than accepting code growth as inevitable. Remove replaced paths, duplicate state, repeated stages, and forwarding layers with no independent ownership; move domain policy out of entry points; and verify that a split reduces the total concepts and implementation that maintainers must understand.

Each first-party source file with an independent responsibility should explain that purpose in a concise file or module comment after the license notice. File-level purpose, type responsibilities, and the contracts of complete stages are distinct; a class or function comment does not replace the file overview. Empty package markers, generated or upstream files, and self-explanatory small partial files do not need mechanical boilerplate.

Comments explain information the code cannot express clearly:

- why stages must run in a particular order;
- who owns a resource or state transition;
- which external contract constrains the implementation;
- what remains true after failure or cleanup.

Every first-party public production function must have a concise doc comment stating what it does, who calls or consumes it, what it returns or publishes, and any ownership or lifecycle responsibility that matters. Private functions that own a complete stage or are not self-explanatory need the same context. This includes program entry points and functions owning startup, request execution, reconciliation, publication, or shutdown. Generated code and third-party sources follow their owning project.

Large code blocks, multi-stage flows, non-obvious algorithms, and critical boundaries must have a small number of structural comments that let maintainers understand the execution before reading details.

Do not comment getters, field forwarding, constant mappings, obvious loops, or each line of a function. Do not record modification history, review replies, temporary plans, or the implementer's reasoning process in code comments.

## Language and repository tools

Follow the surrounding code and the executable checks for each subtree.

- Rust data plane: run `make verify-data-plane`. It checks formatting and compiles all workspace targets with Clippy.
- Go control plane: run `make -C control-plane verify`. Generated code and CRDs must be regenerated through the Makefile targets rather than edited independently.
- Python CLI: keep public argument definitions and parsing separate from command execution, return explicit command types rather than passing `argparse.Namespace` across that boundary, and exercise the affected installed command against Kubernetes.
- Python benchmarks: follow the package structure and typing style already used under `benchmarks/`; run the relevant benchmark tests and exercise the affected benchmark command or runner path.
- Helm and Kubernetes YAML: run Helm lint and render the changed modes. Verify generated resources, values schema, selectors, ports, namespaces, and network access as one deployment contract.
- Repository-wide files: run `pre-commit run --all-files` for file hygiene, structured formats, merge conflicts, secret detection, and GitHub Actions linting.

Generated files must identify or have a documented source. Change the source and regenerate the artifact; do not maintain generated output as a second implementation.

## Testing and continuous integration

The repository-wide testing and CI rules are maintained in [Testing Guidelines](testing.md) and [CI Guidelines](ci.md). They define the key-feature scope, real execution and no-mock boundary, test addition and review steps, CI ownership, trigger selection, artifact validation, cleanup, and merge-gate decisions.

This guide only establishes the code-style boundary: tests and CI must have a current owner, reuse the product's existing lifecycle, and avoid duplicate implementations, private-helper checks, speculative matrices, and tests or jobs with no current consumer.

## Documentation

Write documentation for the person who will use or maintain the final system, not for the current PR discussion or the author's implementation process. Start from the task readers need to complete, then decide which background, commands, choices, and results belong on the page.

Organize information with headings, paragraphs, lists, and tables. Do not use bold emphasis.

Before adding or substantially rewriting user documentation:

1. define the page's responsibility and the result its readers need;
2. verify the current code, CLI help, Helm values, schema, maintained examples, runtime behavior, and cleanup path;
3. read the latest documentation from at least two mature projects with a similar responsibility;
4. outline the shortest executable path before writing explanatory prose.

Compare how mature projects present prerequisites, first success, alternatives, verification, and troubleshooting. Learn from their organization and ownership; do not combine or copy their text. Deprecated, historical, draft, and proposal documents may provide background but are not templates for current guidance.

Give each document one clear responsibility:

- The root README explains the project and provides one default path from setup to a successful request and cleanup.
- Component READMEs describe user-visible purpose, inputs, outputs, access, and necessary limits rather than source-file tours or private contracts.
- Installation, deployment, operations, troubleshooting, and reference guides own their respective details instead of duplicating them across entry pages.
- Maintainer documents may explain ownership, data flow, and trade-offs, but must distinguish current behavior from target or draft designs.

A Quick Start should follow one default success path. Put alternatives where readers actually make that choice, or link to the guide that owns them. Do not interrupt the main path with every installer, cluster type, access mode, hardware backend, registry, or advanced option.

Commands and configuration examples must be directly usable:

- use real maintained paths, model identifiers, fields, and defaults;
- explain placeholders before the command, and avoid them when a repository example can be used;
- show the exact configuration fragment when a field must be added or changed;
- keep commands together only when readers should run them in sequence, with short comments for distinct stages;
- let a precise heading introduce a single obvious command instead of repeating it in prose;
- do not repeat the same command in an overview and again in the detailed steps.

After a command, describe the user-visible result: what is installed or deployed, what resources it needs, and how the reader knows it succeeded. Mention waiting or internal lifecycle details only when they affect when the reader can continue.

When adding a capability, reconsider the reader path instead of mechanically appending another paragraph. State shared behavior once, place differences in a focused section or owned guide, and remove replaced, repeated, or no-longer-actionable content. Use stable directory or reference entry points for lists that will grow, such as examples and contributors.

Documentation review is iterative. Read the complete user path as a first-time user, an experienced platform operator, and a source developer. For each concrete point of friction, fix page responsibility, section structure, and information placement before rewriting sentences. Then reread the surrounding steps and remove any duplication introduced by the change. Repeat until the default path is executable, alternatives appear at their decision point, and no reader has a reachable blocker; do not continue changing wording after those conditions are met.

Before finalizing, perform a separate deletion pass for duplicated commands, meta-commentary about the document, misplaced alternatives, implementation details, and headings without an independent responsibility. Formatting and tests do not replace this reading review.

Keep English and Chinese documentation aligned in capability, prerequisites, commands, defaults, and limitations, but write each version independently for its readers. Use natural technical English and natural Chinese rather than translating sentence by sentence. Preserve exact commands, field names, type names, and Kubernetes resource kinds when readers need to recognize or enter them; explain uncommon terms in the surrounding language on first use.

## Change and PR discipline

Keep each PR focused on one responsibility and make its final diff the smallest maintainable expression of that change.

- Do not mix a feature with unrelated refactoring, dependency upgrades, generated drift, or repository-wide formatting.
- Remove replaced implementations, unused configuration, duplicate defaults, obsolete documentation, and tests that only protected the old path.
- Verify the PR base before review. A stacked PR should identify its predecessor and must not reintroduce files or behavior already replaced on `main`.
- Do not commit credentials, private infrastructure details, personal absolute paths, local caches, temporary outputs, or experiment artifacts.
- Report the commands actually run and their results. State important unverified areas directly; do not claim CI, hardware, Kubernetes, or performance validation that did not occur.

Use [.github/PULL_REQUEST_TEMPLATE.md](../../.github/PULL_REQUEST_TEMPLATE.md) to present the problem, changes, validation evidence, performance, impact, and related issue or proposal without copying this guide into the PR body.
