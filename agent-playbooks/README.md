# Agent Playbooks

Task-oriented playbooks for external agents working on Foretoken inference systems.

Each playbook connects a task goal with the relevant project guidance, execution path, validation, observability, and evidence requirements.

## Layout

- `common/`: guidance shared by all playbooks;
- `guidance/`: architecture, code-entry, reference, and validation guidance;
- `tasks/`: playbooks for specific optimization tasks.

These files support external agents such as Codex or Claude Code. They do not define or host an Agent model.
