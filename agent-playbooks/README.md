# Agent Playbooks

English | [简体中文](README_zh.md)

Task playbooks for Agents improving Foretoken inference systems.

## Why this exists

Foretoken optimization crosses source code, deployment, workload design, runtime behavior, and observability. An Agent can edit a plausible implementation quickly, but a useful change also needs the correct ownership boundary, a real execution path, evidence from the running system, and an explanation of what caused the result.

These playbooks turn that work into a repeatable path. They connect a task goal with the relevant project context, references, allowed change scope, execution steps, validation, diagnosis, and evidence requirements.

## How to use a playbook

1. Select the task that matches the intended optimization.
2. Read the shared rules in `common/`.
3. Read the relevant architecture and validation material in `guidance/`.
4. Follow the task playbook under `tasks/`.
5. Inspect the current code and configuration before editing.
6. Run the real deployment, workload, and observation path described by the playbook.
7. Record the result, including negative results and attribution limits.

The developer chooses the task and reviews the change. The Agent performs the investigation, implementation, execution, and analysis within the stated boundaries.

## What each task playbook contains

Every task playbook should define:

- the goal and success criteria;
- ownership boundaries and relevant code entry points;
- references and required project context;
- allowed changes and non-goals;
- deployment and experiment steps;
- validation, observation, and diagnosis;
- checks for behavioral integrity and causal attribution;
- required evidence and follow-up knowledge.

## Layout

- `common/`: rules shared by all playbooks;
- `guidance/`: architecture, code-entry, reference, and validation guidance;
- `tasks/`: playbooks for specific optimization tasks.
