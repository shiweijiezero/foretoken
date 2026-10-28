<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 绘制 RSI 优化进展

[English](progress-charts.md) | 简体中文

里程碑图展示实测累计版本的变化，搜索图展示全部有效尝试、截至当时的最佳结果和失败尝试。两张图随[实验报告](../experiment-records_zh.md#持续维护用户报告)一起维护。

## 首次生成

使用已安装的 Foretoken CLI。中文标注需要已安装中文字体，例如 Noto Sans CJK SC。在仓库根目录执行：

```bash
mkdir -p results/my-experiment/figures
cp rsi-playbooks/templates/progress-template.yaml results/my-experiment/figures/progress.yaml
```

在 `progress.yaml` 中填写标题、对照条件和更新时间，再按下文填入基线和测量结果。基线尚未测得时，保留 `baseline.value: null`。然后生成图片：

```bash
foretoken plot --progress results/my-experiment/figures/progress*.yaml
```

命令以 PNG、SVG 和 PDF 导出里程碑图与搜索图，例如 `progress-milestones.png` 和 `progress-search.png`。基线尚未测得时生成待测图片。按[报告模板](report-template_zh.md)将 PNG 放在结果汇总表之后。

每个指标和每组对照条件分别使用独立文件，例如 `progress-c1-throughput.yaml` 和 `progress-c16-latency.yaml`。一次导出本报告的所有条件：每次命令会替换输出目录中上次导出的图表文件。输入均在同一目录时默认输出到该目录，来自不同目录时默认输出到 `./plots`；用 `--output-dir` 指定其他位置。输出名沿用各输入文件去掉扩展名后的名称。预览样式时，将[虚构数据示例](progress-example.yaml)复制到临时目录，再对副本执行绘图命令。

## 填入结果

同一文件沿用同一指标、baseline 和对照条件。`subtitle` 写明模型、硬件、精度、负载、并发和测量范围。

| 字段 | 内容 |
| --- | --- |
| `title`、`subtitle` | 标题和对照条件；标题自动加上 `Foretoken · ` 前缀。 |
| `updated_at` | 与报告一致的更新时间，包含时区。 |
| `x_label` | `x` 的含义，例如经过的小时数、评测序号或工具调用次数；baseline 位于零点。 |
| `metric` | `name`、`unit` 和改善方向 `direction`；吞吐通常用 `higher`，延迟通常用 `lower`。 |
| `baseline` | 实测 `value`、`label` 和证据 `source`；尚未测量时保留 `value: null`。 |
| `points` | 每次尝试的 `x`、`value`、`status` 和 `source`；可选 `label` 添加标注，`checkpoint: true` 选为累计里程碑。 |
| `phases` | 可选阶段背景，包含 `label`、`start`、`end`，与 `x` 使用相同单位。 |
| `references` | 同条件对照，包含 `label` 和 `source`；用 `value` 绘制水平线，或用 `points: [{x, value}, …]` 在搜索图中绘制实测轨迹。 |

对照值使用同一指标和 baseline，轨迹点沿用图中的 `x` 单位。例如，`points: [{x: 0, value: 100}, {x: 2, value: 120}]` 记录两次测量，证据由该对照的 `source` 链接。

`source` 填写相对于 YAML 文件的证据路径或 URL。数值采用报告中的汇总结果，并链接相应运行记录。标注自动排布，可用 `label_offset: [x, y]` 微调位置，单位为排版磅。

| `status` | 含义 |
| --- | --- |
| `valid` | 条件可比、符合任务约束的实测值。 |
| `incorrect` | 未通过正确性或质量检查。 |
| `compile_failed` | 构建失败。 |
| `runtime_failed` | 执行失败或没有可用测量。 |

失败尝试显示在独立底栏，`value` 设为 null，并移除 `checkpoint` 或设为 false。更正原先的无效结果时，按此调整数据并修改状态，重新绘图，在迭代笔记中链接更正依据。

是否需要质量评测，按[验证范围说明](../../.agents/skills/rsi/roles/common_zh.md#独立复核结论)判断。改动不影响输出质量，或已有证据仍适用时，可比实测值可直接记为 `valid`。确有与改动相关的质量问题尚未解决时，先在报告和命名 reference 中保留速度测量；reference 不进入历史最佳曲线或里程碑。例如：

```yaml
references:
  - label: 候选速度；输出一致性尚未确认
    source: ../iterations/candidate/notes/analysis.md
    points: [{x: 3, value: 119}]
```

按任务的正确性与质量要求判定结果，另行记录是否保留方案。审查确认失败后，记录 `incorrect` 尝试并在报告中解释；reference 可以更新标注，继续保留观察到的速度。硬件、负载或测量条件不同的数据分别对照。

## 阅读与维护

指标越大越好时，改善倍数为 `value / baseline`；越小越好时为 `baseline / value`。baseline 为 `1×`，倍数越大表示改善越大。

里程碑图连接 baseline 与标为 `checkpoint: true` 的有效点。每个点代表实测的累计配置；同时保留多项改动时，使用组合后的实测值。后续版本替代早期版本时，保留早期节点以呈现历史。

搜索图展示全部有效值，以及从 baseline 起始的历史最佳阶梯线；当前保留版本由报告说明。

恢复任务时沿用同一份数据和 baseline 原点，保留早期已采用版本与失败尝试。后续对照复测用命名 reference 表示，没有新测量的更新保留已有点。
