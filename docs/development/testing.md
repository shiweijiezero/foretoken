<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Testing Guidelines

English | [简体中文](testing_zh.md)

Foretoken maintains a small set of tests for key product features. Tests use real components and dependencies, with end-to-end (E2E) execution wherever possible. A code change does not require a new test.

For example, a multi-model serving scenario deploys the maintained configuration through the CLI, sends requests to both models, checks their responses, and removes the deployment. It verifies the feature through the product rather than constructing a routing table and asserting its fields. This illustrates how to choose a scenario; it does not prescribe a new test.

## Principles

### Select key features, not implementation details

Start with the capability users rely on, then select a few representative scenarios that demonstrate it. Do not derive a test inventory from functions, source files, branches, parameter combinations, or every conceivable failure. Coverage percentages and test counts are not targets.

Do not maintain tests for getters, field forwarding, constant mappings, thin wrappers, private helpers, framework wiring, or upstream internals. Foretoken's integration with an upstream component is in scope when it is part of a selected feature.

### Use real execution without mocks

Do not substitute mock objects, fake clients, stub servers, fabricated engine responses, or monkeypatched behavior for production dependencies. Fixtures may supply inputs and configurations or provision real resources; they must not implement a second version of a component.

Use the actual CLI, services, Kubernetes components, inference engine, and hardware required by the feature. Reuse the maintained installation, deployment, request, benchmark, and cleanup paths. Test orchestration owns scenario setup, execution, assertions, diagnostics, and teardown—not another installer, controller, or request runner.

Prefer the complete user path. When a key feature needs focused verification, execute the real implementation and state the narrower scope. Compilation, template rendering, and a running API server each prove their own boundary, not a working inference service. Missing hardware or dependencies means the scenario was not executed; do not replace it with a simulated pass.

### Check outcomes and keep scenarios cohesive

Assert observable behavior: responses, applied configuration, service state, feature-relevant metrics, and resource cleanup. Avoid private call counts, incidental execution order, complete log snapshots, and JSON/YAML formatting. When inspecting structured resources, assert their semantic fields rather than matching text occurrences.

A scenario may cross modules and contain several related assertions. Keep independent features separate enough to diagnose failures; do not turn E2E into one long script covering unrelated capabilities. Model-quality and performance conclusions require the relevant evaluation or measurement, with explicit workloads and hardware conditions rather than a fixed generated sentence.

### Maintain existing tests before adding more

Run the relevant existing scenarios first. Update their inputs, calls, and expected outcomes when the supported behavior changes, preserving the purpose of each scenario. Adding an independent scenario inside an existing file is still adding a test.

New features, bug fixes, refactors, or temporary investigations do not automatically justify persistent tests. New tests require explicit maintainer approval of their feature scope and maintenance value. A successful one-off validation may remain execution evidence rather than becoming permanent code or CI.

## Adding a test

1. Name the key feature and expected result. Describe the user operation and observable outcome. Read its implementation, owning documentation, and current validation before proposing a scenario.
2. Check existing coverage and necessity. Prefer rerunning or adapting an existing scenario. Explain the specific gap and why repeated automated execution is worth maintaining; obtain maintainer agreement before adding a file, case, subtest, or independent scenario.
3. Choose the real execution path. Identify the existing product entrypoints, required dependencies and hardware, and the smallest representative configuration. Do not widen public APIs or add production switches solely to make tests convenient.
4. Implement the scenario and its cleanup. Use a descriptive feature-oriented name and a brief comment explaining its purpose. Isolate mutable resources between runs, wait for observable conditions with bounded timeouts, and clean up owned resources on success, failure, and cancellation. Retain only useful diagnostics, without credentials or sensitive request content.
5. Run and inspect it. Execute the actual path and inspect responses, state, logs, and cleanup. Confirm that the assertions distinguish the expected result from a concrete wrong result. If recovery is the selected feature, trigger the relevant failure in the isolated real system rather than programming an exception into a substitute.
6. Review before retaining it. Follow the review below, remove temporary investigation code, and record the command, execution conditions, and outcome. CI scheduling is a separate decision under the [CI guidelines](ci.md).

Component tests belong to their owning package, named by behavior rather than mirroring private modules. Rust component tests live in the crate's root `tests/`, not inline `src/` test modules. Go and Python use their standard package test locations. Cross-component E2E orchestration belongs with the owning feature's validation entrypoint; do not create a new generic testing framework or directory hierarchy for a single scenario.

## Reviewing a test

First review correctness and execution fidelity:

- Trace the test entrypoint through the real producers, consumers, and cleanup. Identify what actually runs and whether any substitute bypasses the feature.
- Check that assertions demonstrate the selected feature, not merely process startup, resource existence, or copied configuration. Expected values must not be calculated by duplicating the implementation under test.
- Verify isolation, bounded waiting, and owned-resource cleanup, including failure and cancellation. Inspect actual outputs; skipped execution is not a pass.
- Check that the tested source, dependencies, and artifacts match the change being evaluated. A historical run or a different installation path is not current evidence.

Then independently review scope and maintenance cost:

- Is this still a key feature with a current consumer? Can another scenario cover the same behavior without losing its meaning?
- Remove duplicate assertions, exhaustive parameter matrices, incidental snapshots, unused helpers, and test-only dependencies with no remaining consumer. Do not replace mocks with a home-grown simulator.
- Keep shared preparation small and reuse the product's existing lifecycle. Ordinary refactoring should not require rewriting tests whose feature behavior is unchanged.
- An absent test is not itself a review finding. Identify a concrete product defect separately; fixing it does not automatically require another permanent test.

## Common mistakes to avoid

| Mistake | Preferred approach |
| --- | --- |
| Adding a test for every edit, function, or bug fix | Reuse the selected feature scenarios; justify additions separately |
| Hiding a new scenario inside a large existing test | Review it as an addition, regardless of file placement |
| Calling fake-client or fake-engine execution E2E | Exercise the real dependency and report the actual boundary |
| Reimplementing deployment or inference in fixtures | Reuse the production entrypoints |
| Asserting generated text layout or internal object shape | Check feature outcomes and semantic resource fields |
| Turning every failure branch into a permanent scenario | Select only what is necessary to verify a key feature |
| Making a flaky test pass through retries or longer sleeps | Diagnose product, test, or environment failure; fix or explicitly isolate it |
| Retaining debug scripts, fixtures, and dependencies after their purpose ends | Remove them with the obsolete scenario |

## Further reading

[llm-d's testing requirements](https://github.com/llm-d/llm-d/blob/main/CONTRIBUTING.md) distinguish component, integration, and deployed-system validation. [Kubebuilder's EnvTest reference](https://book.kubebuilder.io/reference/envtest) explains the limits of an API-server-only environment. These are useful for describing execution boundaries; Foretoken's feature selection and no-mock rules are defined above.
