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
| [templates/](templates/README.md) | Templates to refer to when writing experiment and iteration notes |
| [guidance/](guidance/README.md) | References and methods for understanding code, measuring behavior, and diagnosing problems |
| [tasks/](tasks/README.md) | Goals, instructions, and result interpretation for specific optimization tasks |

## Steps

1. Define the behavior to improve and how to judge success, such as reducing time to first token under a given workload while preserving answer quality.
2. Review relevant references, code, and existing measurements. Choose an approach for this iteration and the workload and metrics for comparison.
3. Change the code or configuration and update the inference service through the [source deployment workflow](../docs/custom-deployment.md).
4. Run the selected [evaluations](../benchmarks/README.md) and compare results before and after the change. Use model outputs, logs, and profiling to determine what accounts for the differences.
5. Keep, refine, or revert the change, then choose the next question to investigate. Add reusable findings to the guidance and references.

Record your reasoning, changes, results, and time spent throughout the experiment, referring to the [experiment template](templates/experiment-template.md) and [iteration template](templates/iteration-template.md). See [experiment guidance](guidance/experiments.md) for organizing records and retaining execution evidence.
