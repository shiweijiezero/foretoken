# Inference Optimization Playbooks

English | [简体中文](README_zh.md)

We want agents to work autonomously and independently over sustained periods with Foretoken, improving inference performance, resource efficiency, and service quality.

We provide these playbooks to help developers and agents find references, conduct optimization experiments, and record iteration results. Developers can use them directly or to guide an agent. Methods and findings from experiments can then become part of the shared guidance for future work.

## Design goals

- Fast iteration: guide code changes, rapid deployment, and experimental validation. Reuse environments and caches, choose tests for the current question, and record time spent at each stage to reduce repeated setup and shorten the feedback cycle.
- Reliable conclusions: examine performance metrics alongside actual model outputs and operating conditions to determine whether a change accounts for the observed improvement. Retain the evidence needed to reproduce the result.
- Cumulative learning: preserve the motivation, results, and decisions from successive trials. Find and update references during a task, and turn verified methods into reusable guidance.

## Contents and use

| Directory | Purpose |
| --- | --- |
| [common/](common/README.md) | Shared experiment guidance and note templates |
| [guidance/](guidance/README.md) | References and methods for understanding code, measuring behavior, and diagnosing problems |
| [tasks/](tasks/README.md) | Goals, instructions, and result interpretation for specific optimization tasks |

Start with the shared experiment templates. References and task-specific playbooks will be added over time. Use the [source deployment workflow](../docs/custom-deployment.md) to apply code changes and the [benchmark tools](../benchmarks/README.md) to measure performance and model quality.

## Experiment records

Organize records into experiments, iterations, and runs. For example, an experiment aimed at reducing time to first output might compare two routing approaches. Each approach is an iteration and can include several performance and quality evaluation runs.

- Experiment notes: use the [experiment template](common/experiment-template.md) for the overall goal, comparison method, and findings. Update it as the investigation progresses.
- Iteration notes: use one [iteration template](common/iteration-template.md) per approach to explain the change, expected outcome, results, and next step.
- Run records: retain commands, configurations, and measurements separately for each execution, and link them from the iteration notes.

Keep records under `results/<goal>/<motivation>/`; see [common guidance](common/README.md) for the layout. Notes explain choices and conclusions, while execution evidence stays with its individual run.
