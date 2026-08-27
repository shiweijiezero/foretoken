# 为 Foretoken 做贡献

[English](CONTRIBUTING.md) | 简体中文

感谢你为 Foretoken 贡献代码、文档、测试、缺陷报告或设计讨论。每项合入的改动都应有明确边界，并且能够解释、验证和长期维护。

所有参与者都必须遵守 [Foretoken 社区行为准则](CODE_OF_CONDUCT.md)。修改代码、配置、测试或用户文档前，请先阅读 [Foretoken 代码风格](docs/development/code-style_zh.md)。

## 开始之前

以下局部修改可以直接提交：

- 文档、拼写和链接修复；
- 有明确复现步骤的缺陷修复；
- 不改变外部行为的局部整理；
- 为已有行为补充测试。

以下修改应先创建 Issue 或设计提案：

- 新增或修改 CRD、CLI、配置格式或公开的 Go/Python API；
- 新增控制器、路由器、扩缩容器、运行时后端或硬件后端；
- 修改控制面与数据面之间的协议；
- 引入外部依赖、测试方法或常驻 CI 任务；
- 重组组件或修改部署方式；
- 可能影响兼容性、性能结果或资源开销。

缺陷 Issue 应包含复现步骤、预期行为、实际行为、运行环境和最小相关日志。

重大修改应先创建标题以 `[Proposal]` 开头的 Issue，并说明：

- 问题、背景和使用场景；
- 目标、非目标和受影响组件；
- 拟议接口或数据流；
- 考虑过的其他方案及放弃原因；
- 兼容性、升级和回退方式；
- 验证、可观测性和成功标准；
- 依赖、CI 成本和长期维护责任。

开始实现前，应取得受影响组件维护者的认可。提案获批只表示方向得到认可，后续代码仍需正常评审。

## 仓库目录

- `data-plane/`：请求处理、路由、推理引擎接入和运行时数据路径；
- `control-plane/`：期望状态、实例生命周期、扩缩容决策、Kubernetes 资源和故障恢复；
- `benchmarks/`：正确性、工作负载、性能、SLO 评估和仿真；
- `deploy/`：部署组合、硬件配置和发布制品。

模块级单元测试应与源码放在一起。可以归属于单个模块的测试不应放入根目录 `tests/`。

## 提交 Pull Request

外部贡献者应从 fork（派生仓库）提交 PR。有主仓库写权限的维护者可以为每个 PR 在主仓库创建一个短期分支。不要额外创建用于 CI 中转或刷新检查的分支；PR 合并或关闭后应立即删除源分支。主仓库中的其他分支只用于承载明确的长期开发任务及其多个相关 PR。

使用仓库的 [Pull Request 中文模板](.github/PULL_REQUEST_TEMPLATE_zh.md)。每个 PR 只承担一项职责，并从最终差异中删除无关格式化、生成文件漂移、本地产物和过时路径。

面向用户的行为、命令、配置或状态发生变化时，应同步更新相关中英文文档和示例。PR 尚未准备好接受完整评审时，请标为草稿状态。

不要提交密钥、令牌、服务器地址、私有 kubeconfig、模型凭据、个人绝对路径或本地实验数据。

提交者对全部代码负责，包括 AI 辅助改动。应检查每一处修改，核对来源和许可证，并且只报告实际执行过的命令和验证。不得将私有代码、凭据、服务器配置或未公开数据发送给外部模型。

合并前，PR 必须通过与改动相关的检查，并获得至少一名非作者维护者的批准。

## 分支与提交信息

分支名称应简短说明目的，例如：

```text
feature/control-plane-baseline
fix/router-timeout
docs/contributing-guide
benchmark/slo-simulation
```

提交信息遵循 Conventional Commits 规范：

```text
feat(control-plane): add inference group reconciliation
fix(router): handle unavailable backends
test(bench): cover SLO search boundaries
docs: add development guidelines
```

常用前缀包括 `feat`、`fix`、`docs`、`test`、`refactor`、`perf`、`ci` 和 `chore`。提交信息应描述实际变化，不要使用笼统的 `update` 或 `fix issues`。

## 许可证

提交到 Foretoken 的内容将按照仓库的 [Apache License 2.0](LICENSE) 发布。请确保你有权提交相关代码、文档、数据和测试材料。
