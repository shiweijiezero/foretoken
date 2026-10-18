---
name: foretoken-code-review
description: Review Foretoken code or a PR through mandatory ordered steps covering user-facing design, correctness, and maintainability. Use when asked to review code or a PR; fix or publish only when requested. Do not run a review when discussing or editing this skill.
argument-hint: "[PR number or URL | branch | path | current changes]"
---

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Code review in Claude Code

English | [简体中文](SKILL_zh.md)

Read `${CLAUDE_SKILL_DIR}/../../../.agents/skills/foretoken-code-review/SKILL.md` and execute its numbered workflow in order, including the per-check evidence and completion conditions. Use `$ARGUMENTS` as the review target or derive it from the conversation as the shared workflow specifies.
