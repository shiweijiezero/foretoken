# Inference Optimization Playbooks

English | [简体中文](README_zh.md)

We want agents to work autonomously and independently over sustained periods with Foretoken, improving inference performance, resource efficiency, and service quality.

We provide these playbooks to help developers and agents find references, conduct optimization experiments, and record iteration results. Developers can use them directly or to guide an agent. Methods and findings from experiments can then become part of the shared guidance for future work.

## Design goals

- Rapid deployment: apply code changes quickly to inference services in the target cluster.
- Testing and feedback: measure performance, model quality, and resource costs to evaluate the effects of a change.
- Observation and diagnosis: use logs, metrics, and profiling to locate runtime problems and performance bottlenecks.
- Experiment records: connect each iteration's changes, configurations, results, and decisions for comparison and reproduction.
- References and shared experience: find and update references, and turn experimental findings into reusable guidance.
- Behavior and attribution checks: examine actual model behavior, identify metric gaming, and determine whether gains come from the intended change.

## Directory structure

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
