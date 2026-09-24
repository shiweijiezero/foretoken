<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Release 描述模板

将下面的模板复制到 GitHub Release 描述中，然后删除不适用的章节和所有注释。正文只保留用户理解、安装、升级和验证本版本所需的信息；如果链接能帮助读者追溯实现，再为用户可见的变更附上 PR 或 Issue。

```markdown
## 主要亮点

- [面向用户的新能力或重要行为变化。] ([#PR](URL))
- [重要的可靠性、性能、平台或文档改进。] ([#PR](URL))

## 变更内容

### 新功能

- [说明新增能力及适用的用户。] ([#PR](URL))

### 改进

- [行为、性能、可观测性、部署或开发体验方面的改进。] ([#PR](URL))

### 修复

- [说明用户可见的问题及修复后的行为。] ([#PR](URL))

### 文档

- [新增或修正文档。] ([#PR](URL))

## 兼容性

| 范围 | 本版本 | 说明 |
| --- | --- | --- |
| Python | [版本范围] | [运行时要求或打包说明] |
| Kubernetes | [支持范围] | [相关部署限制] |
| NVIDIA | [支持的运行时/镜像] | [兼容性或镜像说明] |
| 沐曦 | [支持的运行时/镜像] | [兼容性或镜像说明] |
| API 与配置 | [兼容 / 已变化] | [字段、接口或协议说明] |

## 破坏性变更与弃用

- [删除、重命名或改变行为的接口。] 请改用[替代方式或迁移动作]。

## 升级说明

1. [需要更新的版本、镜像、Chart、CRD 或配置。]
2. [需要执行的命令或迁移动作。]
3. [默认行为或回滚方式发生变化时，说明运维者需要采取的动作。]

## 发布产物

| 产物 | 版本或 tag | 安装或访问方式 |
| --- | --- | --- |
| Python package | `foretoken==X.Y.Z` | `pip install foretoken==X.Y.Z` |
| OCI 镜像 | `X.Y.Z` | `ghcr.io/shiweijiezero/foretoken/<name>:X.Y.Z` |
| 沐曦 model-server | `X.Y.Z-metax` | `ghcr.io/shiweijiezero/foretoken/model-server:X.Y.Z-metax` |
| Helm Chart | `X.Y.Z` | `oci://ghcr.io/shiweijiezero/foretoken/charts` |
| 示例 | `vX.Y.Z` | [Release tag 中的 examples](https://github.com/shiweijiezero/foretoken/tree/vX.Y.Z/examples) |

## 已知限制

- [会影响本版本安装、升级或使用方式的当前限制。]

## 致谢

- [@contributor](URL) — [具体说明代码、测试、文档、问题定位、硬件或其他支持内容]。
- 感谢所有提交问题、评审变更、验证版本或帮助改进 Foretoken 的贡献者。

## 完整变更记录

[比较 `vPREVIOUS` 与 `vX.Y.Z`](https://github.com/shiweijiezero/foretoken/compare/vPREVIOUS...vX.Y.Z)
```

## 编写规则

- “主要亮点”写三到五项结果，不写提交记录列表。
- “变更内容”按用户能理解的领域归类，把同一能力的多个 PR 合并成一条说明。
- 如果读者需要选择运行时、修改配置或在使用前执行动作，保留“兼容性”和“升级说明”。
- 发布 Python package、各 OCI 镜像变体、Helm Chart 和带版本 tag 的示例时，逐项列出对应产物。
- “已知限制”只保留会改变用户操作的当前限制；没有此类限制时删除整节。
- 只根据已确认的贡献或支持记录署名，并写明具体帮助内容。
- 完整提交历史放在 compare 链接中，正文不展开每个内部提交。
