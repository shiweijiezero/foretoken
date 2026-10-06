# Inference Optimization Playbooks

English | [简体中文](README_zh.md)

Guidance and experiment notes for improving Foretoken inference systems, connecting code changes with deployment, workloads and measured results.

Use the [shared templates](common/README.md) to describe an experiment and its iterations. Deploy through the [source workflow](../docs/custom-deployment.md) and measure with the existing [benchmark tools](../benchmarks/README.md).

## Organization

| Directory | Purpose |
| --- | --- |
| [common/](common/README.md) | Experiment and iteration notes shared by all tasks |
| [guidance/](guidance/README.md) | Reusable architecture, code and measurement guidance |
| [tasks/](tasks/README.md) | Instructions for individual optimization tasks |

## Writing a task playbook

Define the goal, change scope and relevant code, then provide the deployment and measurement steps needed to compare with a reference result. Explain how to interpret the observations and decide whether to keep the change. Link shared guidance and existing commands rather than copying them.
