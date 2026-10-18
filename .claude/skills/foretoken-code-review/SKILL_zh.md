---
name: foretoken-code-review
description: 按强制顺序审查 Foretoken 代码或 PR，覆盖用户接口设计、正确性和可维护性。用户要求审查代码或 PR 时使用；修复与发布按对应授权执行。讨论或编辑本 skill 时不启动审查。
argument-hint: "[PR 编号或 URL | 分支 | 路径 | 当前改动]"
---

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 在 Claude Code 中审查代码

[English](SKILL.md) | 简体中文

读取 `${CLAUDE_SKILL_DIR}/../../../.agents/skills/foretoken-code-review/SKILL_zh.md`，按编号顺序执行共享流程，逐项记录证据并满足完成条件。以 `$ARGUMENTS` 为审查对象；没有参数时，按共享流程从对话确定范围。
