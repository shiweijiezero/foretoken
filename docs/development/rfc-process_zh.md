<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# RFC 流程

[English](rfc-process.md) | 简体中文

当需要讨论新的跨组件方向或平台能力时，应在实现前使用 RFC。RFC 以 GitHub Issue 形式存在，不能替代实现 PR。

## 什么时候创建 RFC

在引入新的平台能力、跨组件架构、公开生命周期或协议之前，应创建 RFC。局部缺陷修复、局部整理和文档修改通常不需要 RFC，除非它们暴露了更广泛的设计决策。

## 创建 RFC

创建标题格式如下的 GitHub Issue：

```text
RFC: <简短标题>
```

内容应包括：

- 动机和用户场景；
- 目标和非目标；
- 受影响组件及其 ownership；
- 拟议流程、接口或生命周期；
- 替代方案及放弃原因；
- 兼容性、发布和回退方式；
- 验证、可观测性和成功标准；
- 依赖和长期维护责任。

## 讨论和实现

与受影响组件的维护者在 Issue 中讨论设计。Issue 应聚焦拟议方向，设计发生变化时及时更新。方向获接受后，再创建聚焦的实现 PR；PR 应链接 RFC，并说明实际实现范围。

RFC 获批只表示方向得到认可，不代表代码已经获批。实现 PR 仍需正常评审、验证、生成产物检查和文档更新。

## 示例

拟议的 Agent-native Inference Infrastructure 应先创建一个 GitHub RFC，描述整体架构；随后再创建聚焦的 PR 引入 `agent-playbooks/` 目录，并在 PR 中链接该 RFC。
