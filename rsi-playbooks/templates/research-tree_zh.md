<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Proposal 树

[research-tree-template.html](research-tree-template.html) 是给用户看的 Proposal 树页面：单文件、零依赖、可离线打开。模板只包含渲染逻辑，数据由脚本注入：

```bash
python3 .agents/skills/rsi/scripts/research_tree.py tree.json results/<task>/figures/progress/research-tree.html
```

用示例数据预览：

```bash
python3 .agents/skills/rsi/scripts/research_tree.py \
  rsi-playbooks/templates/research-tree-example.json /tmp/research-tree.html
```

## 树的结构

每一轮：本轮 baseline → 方案池中的 Proposal → 实验（叶子）。Proposal 细化自同一轮的另一个 Proposal 时挂在它下面；跨轮的细化挂在本轮 baseline 下，并用虚线连到原 Proposal。合版是 `mergeOf` 非空的 Proposal，完成后成为下一轮的 baseline，树随轮次向右扩张。

页面自己计算实验显示名（`实验-<Proposal>-<次数>`）、轮次和布局。

## 数据字段

顶层：

| 字段 | 内容 |
| --- | --- |
| `task` | 任务说明，键值对，显示在原始 baseline 面板 |
| `updated_at` | 数据生成时间 |
| `taskRoot` | 从生成的 HTML 到 RSI 任务目录的相对路径；下面所有路径都相对任务目录 |
| `scopes` | 比较条件 `[{id, label}]` |
| `metrics` | 指标 `[{key, label, unit, dir}]`，`dir` 为 `up`（越大越好）或 `down` |
| `baseline` | 原始 baseline：`name`、`code`、`m`、`links`（`{显示名: 路径}`） |
| `proposals` | 方案池的当前视图 |
| `experiments` | 实验池的当前视图 |

测量 `m` 的形状为 `{<scope id>: {<metric key>: 均值, "n": 重复次数}}`，未测的条件省略。`m` 和 `retained` 由生成程序从实验 `recordDirectory` 中的 `progress.yaml` 和 run 记录读出。

方案池字段在 Proposal 详情面板里全部显示。Proposal 另加：

| 字段 | 内容 |
| --- | --- |
| `baseName` | 基于哪个合版 Proposal；`null` 为原始 baseline |
| `parentName` | 细化自哪个 Proposal；没有为 `null` |
| `mergeOf` | 合版包含的实验名；普通 Proposal 为空 |

实验使用实验池字段 `name`、`description`、`commands`、`recordDirectory`、`state`，另加：

| 字段 | 内容 |
| --- | --- |
| `proposalName` | 所属 Proposal |
| `measured` | 实测 parent 的实验名；`null` 为本轮 baseline |
| `retained` | 实验者记录该实验在帕雷托前沿上 |
| `m` | 测量 |

状态原样取自两个池：

| Proposal `state` | 显示 | 实验 `state` | 显示 |
| --- | --- | --- | --- |
| `draft`、`rejected` | 不上树 | `pending` | 等待执行 |
| `ready` | 待实现 | `running` | 执行中 |
| `implementing` | 实现中 | `completed` | 已完成 |
| `completed` | 已完成 | `failed` | 失败 |
| `abandoned` | 已放弃（连同其实验剪枝） | | |

实验失败表示执行出错或被撤销，不剪枝。合版的测量取它最近一次完成的实验。
