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
| [templates/](templates/README.md) | Experiment layout, record templates, and technique note templates |
| [guidance/](guidance/README.md) | Reference material and knowledge for inference-system optimization |
| [tasks/](tasks/README.md) | Goals, environments, and scope for common optimization tasks |

## Steps

1. Define the behavior to improve and how to judge success, such as reducing time to first token under a given workload while preserving answer quality.
2. Set the scope: focus on routing, the inference engine, or another specific area, or leave it open to changes across the whole system.
3. Read the code and relevant research and practice. Use existing measurements to understand the system's current behavior, taking additional measurements as needed.
4. Analyze the causes and opportunities for improvement, develop your own hypotheses, and design an approach. State the expected effects and choose workloads and metrics that test those hypotheses. If comparable results are unavailable, measure the service before making the change.
5. Change the code or configuration and update the inference service through the [source deployment workflow](../docs/custom-deployment.md).
6. Run the selected [evaluations](../benchmarks/README.md) and compare results before and after the change. Use model outputs, logs, and profiling to determine what accounts for the differences.
7. After each evaluation, the developer or agent adds the run link, changes, result interpretation, and time spent to `results/<goal>/<motivation>/iterations/<name>/notes/iteration.md`. At the end of the iteration, update the experiment root’s `notes/experiment.md`. Refer to the [note templates](templates/README.md) and [record layout](templates/experiments.md).
8. Keep, refine, or revert the change, then choose the next question to investigate. Add reusable findings to the guidance and references.
9. Optional: review the implementation and raw results for benchmark-specific shortcuts, changes in model behavior, or differences in comparison conditions, and check whether the claimed gains are attributable to the intended change.
