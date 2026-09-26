<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# CI Guidelines

English | [简体中文](ci_zh.md)

Continuous integration (CI) schedules existing repository checks and selected key-feature tests, provides their execution environments, and reports their results. It does not own a second implementation of building, deploying, or testing Foretoken. Feature selection and real execution follow the [testing guidelines](testing.md).

For example, the data-plane workflow calls `make verify-data-plane` rather than maintaining a separate list of Rust verification commands. A deployed-feature scenario should likewise have one owning entrypoint that developers and CI can both invoke. A documentation edit should not require a GPU run merely because the repository serves models.

## Separate responsibilities

| Responsibility | Purpose | Execution scope |
| --- | --- | --- |
| Basic checks | Formatting, static analysis, compilation, and generated-artifact consistency | Relevant changes, with fast feedback |
| Key-feature E2E | Real product behavior through the maintained entrypoints | Selected features and their actual environment requirements |
| Release automation | Build, validate, and publish distributable artifacts | The release being prepared, reusing existing build and validation paths |
| Notifications | Deliver workflow or repository activity | Report results without implementing checks or controlling product behavior |

A test's existence, its CI schedule, and its status as a required merge check are separate decisions. Do not add permanent jobs, workflows, matrix dimensions, scheduled runs, or merge gates by default. A feature, bug fix, temporary incident, or one-time migration does not automatically justify any of them.

## Design principles

### Keep workflows thin and reproducible

Workflows prepare the environment, invoke the owning repository commands, collect diagnostics, and ensure cleanup. Keep feature assertions and deployment lifecycle logic in their owning validation entrypoint, not in long YAML shell blocks. Developers must be able to reproduce the same operation outside the CI provider with its documented dependencies.

Reuse setup and build paths already owned by the repository. Reuse a build artifact between compatible jobs when it represents the same evaluated source and dependency combination; do not rebuild it independently in every test job or run a stale cached binary. Cache dependencies and compilation work, not successful test outcomes.

### Select work by impact and cost

Choose tasks from the affected feature and its dependencies. Path filters must account for shared protocols, dependency manifests and locks, build configuration, deployment configuration, and the validation entrypoint itself—not only the nearest source directory. When impact is uncertain, select the broader relevant existing task rather than silently omitting it.

Keep fast checks distinct from expensive cluster, GPU, multi-node, and performance runs. Select representative combinations instead of the Cartesian product of models, hardware, topology, and parameters. Use on-demand execution where appropriate; add a periodic run only for a specific need that existing execution does not meet. Hardware used for one validation is an execution condition, not a universal user requirement.

Cancel superseded verification runs when their results are no longer needed, while preserving cleanup. Do not apply the same cancellation policy blindly to release publication or shared-environment operations.

### Verify the delivered artifacts

Run the package or image built from the change under evaluation. Check that installation, startup, and the selected feature actually consume that artifact through the normal product path. Source execution alone does not validate a wheel or image; manual repair inside a running container does not validate the distributed image.

Real E2E requires the feature's actual dependencies and hardware. If those are unavailable, report that execution did not occur. Do not replace it with mocks, turn an error into an empty result, or report a skipped scenario as passed.

### Make failures actionable and runs isolated

Report what ran, its source/artifact identity, relevant environment conditions, and its outcome. Separate product failures from setup failures and skipped execution. Keep useful command output and component diagnostics; exclude credentials, sensitive payloads, and private infrastructure details from public logs.

Each run owns its mutable resources and cleans them up on success, failure, and cancellation. Shared runners must not let one run change another run's state or delete another workload. Use existing identity and lifecycle mechanisms rather than introducing content hashes, frozen baselines, or a new resource-management framework.

Do not use blanket retries, unconditional success, or swallowed exit codes to make a job green. Diagnose flaky behavior; explicitly disable or isolate an unreliable check while it is repaired rather than presenting it as a reliable pass.

### Keep execution permissions narrow

Use only the credentials and permissions required by the job. Untrusted pull-request code must not run with publishing credentials or unrestricted access to shared GPU runners and clusters. Select a trusted execution context or an isolated environment; do not use a privileged event merely to bypass fork restrictions.

## Adding or changing CI

1. Identify the responsibility. State the basic check, selected key feature, or release artifact being validated. Inspect the current workflow and its owning commands first.
2. Establish necessity. Explain what existing execution misses, why automation is useful, expected runtime and resource use, and who will diagnose failures. Prefer adjusting an existing task. Obtain maintainer approval before adding a permanent job, workflow, matrix dimension, or scheduled run.
3. Choose triggers and environments. Include shared dependencies in the impact selection. Specify actual hardware requirements, trust boundaries, concurrency, timeouts, and resource cleanup. Decide whether on-demand execution is sufficient.
4. Reuse the repository entrypoint. Keep the workflow as orchestration. If an approved scenario needs an entrypoint, place it with its owning validation rather than writing a second implementation in CI.
5. Validate the execution. Run the owning command in the intended environment and inspect actual outputs and cleanup. Exercise the changed CI wiring in an authorized run; local syntax checks alone do not establish that runner permissions, artifact transfer, or triggers work. Report any part not executed.
6. Review and decide merge requirements separately. Follow the review below. A required check needs explicit agreement on its necessity, reliability, cost, failure owner, and behavior when skipped. Do not silently expand branch protection as part of adding a job.

## Reviewing CI

First trace execution and correctness:

- Follow event and change selection through setup, build, artifact transfer, invocation, result reporting, and cleanup. Check that relevant changes reach the intended tasks.
- Confirm the tested artifact is the intended one, required dependencies are real, and permissions match the event's trust level.
- Inspect failure and cancellation behavior, timeouts, logs, and cleanup. A missing environment, skipped dependency, or failed setup must not appear as a successful feature test.
- Verify required-check behavior when jobs are filtered, skipped, or cancelled so it neither misrepresents validation nor leaves an expected check indefinitely pending.

Then independently review necessity and simplicity:

- Remove duplicate builds, copied deployment lifecycles, assertions embedded in workflow YAML, and obsolete jobs or matrix combinations.
- Ask whether each task has a current purpose distinct from existing checks. Do not create permanent infrastructure for a temporary investigation.
- Check whether representative feature runs provide the needed evidence without a larger matrix, another scheduler, or a new merge gate.
- Preserve the responsibilities of release and notification workflows; do not remove them merely because they share the workflow directory with tests.

## Common mistakes to avoid

| Mistake | Preferred approach |
| --- | --- |
| A new workflow or matrix dimension for every change | Reuse existing tasks and justify permanent additions |
| Long YAML scripts containing product assertions and deployment logic | Invoke the repository entrypoint that owns the operation |
| Checking rendered resources with text counts | Use semantic assertions in the owning validation path |
| Rebuilding the same artifact in each job | Reuse the artifact for the same evaluated combination |
| Running only the changed source directory's checks | Include affected shared dependencies and delivery configuration |
| Calling compilation, rendering, or dry-run output E2E | Report that check's actual scope and run the real feature path |
| Skipping GPU execution while reporting the feature passed | Report the missing execution explicitly |
| Adding retries or ignoring exit codes to stabilize CI | Diagnose the failure and keep its outcome visible |
| Automatically making each new job a required check | Decide merge requirements separately with explicit approval |

## Further reading

[Dynamo's PR workflow](https://github.com/ai-dynamo/dynamo/blob/main/.github/workflows/pr.yaml) illustrates change-based selection, hardware-specific execution, and cancellation of superseded runs. [vLLM's contribution guide](https://docs.vllm.ai/en/latest/contributing/) describes selective CI execution under limited compute resources. Borrow their separation of responsibilities, not their infrastructure size or complete job matrices.
