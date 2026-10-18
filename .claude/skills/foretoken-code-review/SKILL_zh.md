---
name: foretoken-code-review
description: 审查 Foretoken 代码或 PR 的用户接口、系统设计、正确性、可维护性、文档和验证。逐项应用仓库规范，检查完整功能及同类问题。修复与发布按对应授权执行；讨论或编辑本 skill 时不启动审查。
argument-hint: "[PR 编号或 URL | 分支 | 路径 | 当前改动]"
---

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 在 Claude Code 中审查代码

[English](SKILL.md) | 简体中文

读取 `${CLAUDE_SKILL_DIR}/../../../.agents/skills/foretoken-code-review/SKILL_zh.md`，逐个维度、逐条适用标准进行审查。以 `$ARGUMENTS` 为审查对象；没有参数时，根据对话和本地改动确定范围。
