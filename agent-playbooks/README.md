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

1. Choose an optimization goal. Refer to the [experiment template](templates/experiment-template.md) to record the question, expected outcome, and comparison method.
2. Review relevant references and code, propose a change, and apply it to the inference service through the [source deployment workflow](../docs/custom-deployment.md).
3. Select [evaluations](../benchmarks/README.md) that test the current approach, then examine model outputs, metrics, and logs to assess its effects.
4. Refer to the [iteration template](templates/iteration-template.md) to record changes, results, and conclusions. Retain execution evidence as described in the [experiment guidance](guidance/experiments.md).
5. Keep, refine, or revert the change based on the results. Continue with the next iteration and add reusable findings to the task guidance and references.
