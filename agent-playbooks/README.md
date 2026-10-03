# Agent Playbooks

English | [简体中文](README_zh.md)

Task-oriented playbooks for external agents working on Foretoken inference systems.

Each playbook connects a task goal with the relevant project guidance, execution path, validation, observability, and evidence requirements.

## Layout

- `common/`: guidance shared by all playbooks;
- `guidance/`: architecture, code-entry, reference, and validation guidance;
- `tasks/`: playbooks for specific optimization tasks.

These files support external agents such as Codex or Claude Code. They do not define or host an Agent model.

The broader design direction is proposed in [RFC: Agent-native Inference Infrastructure](https://github.com/shiweijiezero/foretoken/issues/242).
