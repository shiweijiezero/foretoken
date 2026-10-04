# Agent Playbooks

English | [简体中文](README_zh.md)

Task playbooks for improving Foretoken inference systems.

## Why this exists

An inference optimization often crosses source code, deployment, workload design, runtime behavior, and observability. The relevant code, commands, metrics, and examples are easy to lose across those boundaries.

These playbooks keep the useful context together and provide a known path from an optimization idea to a measured result. They can be followed by a person or an Agent.

## How to use a playbook

1. Select the task that matches the intended optimization.
2. Read the shared material in `common/` and the relevant guidance in `guidance/`.
3. Inspect the current code and configuration.
4. Make the change and deploy it through the normal source workflow.
5. Run the workload, benchmark, or profile described by the task.
6. Compare the result with the reference case and record what changed.

## What each task playbook contains

Every task playbook should define:

- the goal and success criteria;
- ownership and relevant code entry points;
- references and project context;
- change scope and non-goals;
- deployment and experiment steps;
- validation, observation, and diagnosis;
- result interpretation and follow-up work.

## Layout

- `common/`: material shared by all playbooks;
- `guidance/`: architecture, code-entry, reference, and validation guidance;
- `tasks/`: playbooks for specific optimization tasks.
