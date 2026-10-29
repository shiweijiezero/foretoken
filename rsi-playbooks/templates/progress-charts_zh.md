<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 绘制 RSI 优化进展

[English](progress-charts.md) | 简体中文

各 Experimenter 记录自己的运行与限定范围的判断，现有绘图命令据此生成共同总表、原生里程碑图和搜索图：

```bash
foretoken plot --progress results/my-experiment
```

结果位于 `results/my-experiment/figures/progress/`：`progress-summary.md` 是可读摘要，`progress-summary.csv` 保存统计与判断，`progress-runs.csv` 提供运行、配置、环境及源码证据入口。Markdown 直接展示对应图表。任务报告链接此摘要；不设中央成员抄写数值，指定的总览负责人仍在报告锁内维护元数据和简短说明。

## 记录一个方案

每个方案将 `progress.yaml` 放在自己的 `notes/iteration.md` 旁。最初配置的记录如下：

```yaml
label: Initial configuration
baseline: true
measurements:
  random-concurrency-1:
    runs: [../runs/perf-000001]
    status: valid
```

后续候选记录真正测量过的父配置及判断：

```yaml
label: + QKV views
measurements:
  random-concurrency-1:
    runs: [../runs/perf-000001]
    parent: ../../baseline/runs/perf-000001
    status: valid
    retained: true
```

路径相对本记录；run 可以指向命令目录或一个具体的保存结果目录。在 `runs` 中列入其他可比重复，原有结果读取器计算均值、样本数和标准差，不在此文件填写数值。由 `iteration.md` 链接解释，由 `team/evaluations/` 链接全新 Evaluator 的评估。

| 字段 | 含义 |
| --- | --- |
| `label` | 完整实测配置，或相对具名父方案的改动。 |
| `baseline` | 仅标记用户最初配置，每个 scope 一项；默认 false。 |
| `measurements` | 从比较范围 scope 到所选运行和判断的映射。 |
| `runs` | 一个方案、一个负载的既有命令／结果目录；发生在评测前的失败可省略此项，由 `iteration.md` 链接构建／诊断日志。 |
| `status` | `valid`、`reference`、`incorrect`、`compile_failed` 或 `runtime_failed`。 |
| `retained` | 是否在此 scope 采用该实测版本；默认 false，采用项必须为 `valid`。 |
| `parent` | 实测父方案的命令／结果目录。采用的候选必须填写，最初 baseline 不需要。 |

`random-concurrency-1` 这样的 scope 说明负载和比较条件。不同协议、缓存条件或硬件条件分别记录，不混为重复测量。命令核对同一 scope 及其父方案已知的负载字段，存在冲突时要求分开记录；硬件变化保留在原始环境证据中，不因此拒绝历史参照。相对最初 baseline 的百分比描述已有观测，表中另保留明确测量过的局部父配置及其变化。该 scope 没有原始 baseline 时保留绝对值，图中显示基线未测量。

搜索图用半透明紫点表示 Correct eval、橙点表示 Incorrect result、红叉表示 Compile failure、灰方表示 Runtime failure；下方失败标记没有性能数值，灰蓝参照不进入最佳曲线。

速度已测得而适用的输出或质量问题未解决时使用 `reference`。它出现在搜索图中，但不进入最佳曲线或采用里程碑。失败判断在没有指标时仍显示。采用由责任人根据适用证据记录，命令不会自动选择最快点。

每个 scope 顶部的“Measured retained branch tips”列出已测采用分支末端及其实际运行、配置和现有源码记录，方便选择后续研究的适用父配置。它沿记录中的父关系识别仍被采用后继继承的祖先，保留祖先历史；独立分支分别列出，不按吞吐自动排名，也不宣称存在唯一最优配置。

生成的采用图按实测父运行连边。A、B 从 P 分别测量时保持两个分支，不能据此建立 A+B。父方案未纳入本 scope 的绘图记录时，该采用点不连线。运行表保留已知的源码／配置入口及缺失证据；客户端 checkout 快照本身不能证明部署源码。

## 刷新共同视图

每份 `progress.yaml` 由该迭代唯一的 Experimenter 维护，其他作者的分析留在各自文件中；不同迭代使用不同记录。同一迭代禁止并写。用现有原子写入函数发布完整记录，避免绘图调用读到截断中的文件。JSON 是合法的 YAML，可直接用于这个 `.yaml` 文件。以下命令在已完成运行、已有 `notes/` 目录后执行，路径换成自己的实际迭代：

```bash
python - <<'PY'
from benchmarks.results.recording.files import write_json

write_json("results/my-experiment/iterations/qkv/notes", "progress.yaml", {
    "label": "+ QKV views",
    "measurements": {
        "random-concurrency-1": {
            "runs": ["../runs/perf-000001"],
            "parent": "../../baseline/runs/perf-000001",
            "status": "valid",
            "retained": True,
        },
    },
})
PY
```

原子替换后，任一 Experimenter 都可运行前述绘图命令。目录模式在既有发布锁内发现和读取所有 owner 的记录，再刷新完整视图；排队的调用取得锁后重新读取。同一文件的 single-writer 规则避免截断写入与读取竞争；多个迭代同时更新时，待负责人完成后再重绘一次以得到稳定快照。

默认图显示输出吞吐与平均 TPOT。用已有选项选择其他保存指标：

```bash
foretoken plot --progress results/my-experiment --metric ttft --stat p95
```

以 `_seconds` 结尾的指标按越低越好计算倍数，其他所选指标按越高越好；需要其他优化方向时使用显式图表 YAML。自定义指标选择会刷新该视图；共享输出目录的作者使用同一选择，或用不同 `--output-dir` 分开保存。恢复时沿用最初配置参照；后续工作从适用的已验证版本继续，并记录真正运行的父配置。

## 已有图表 YAML

使用已安装的 Foretoken CLI。中文标注需要已安装中文字体，例如 Noto Sans CJK SC。在仓库根目录执行：

```bash
mkdir -p results/my-experiment/figures
cp rsi-playbooks/templates/progress-template.yaml results/my-experiment/figures/progress-main.yaml
```

在 `progress-main.yaml` 中填写对照条件和更新时间，再按下文填入基线和测量结果。基线尚未测得时，保留 `baseline.value: null`。然后生成图片：

```bash
foretoken plot --progress results/my-experiment/figures/progress*.yaml
```

命令以 PNG、SVG 和 PDF 导出里程碑图与搜索图，例如 `progress-main-milestones.png` 和 `progress-main-search.png`。基线尚未测得时生成待测图片。按[报告模板](report-template_zh.md)将对应 PNG 紧接在总表上方。

每个指标和每组对照条件分别使用独立文件，例如 `progress-concurrency-1-throughput.yaml` 和 `progress-concurrency-16-latency.yaml`。每次刷新只替换所选 YAML 对应的图表，保留输出目录中其他条件的图表。输入均在同一目录时默认输出到该目录，来自不同目录时默认输出到 `./plots`；用 `--output-dir` 指定其他位置。输出名沿用各输入文件去掉扩展名后的名称。预览样式时，将[虚构数据示例](progress-example.yaml)复制到临时目录，再对副本执行绘图命令。

## 填入结果

用户最初的初始配置是唯一 baseline。同一文件沿用同一指标和可比负载条件；后续控制放在 `references`，不重置 baseline。每个点对应总表中的一个方案，使用相同名称和汇总值；重复运行采用表中的统计方式，链接原始测量。标题和副标题只补充坐标轴或报告尚未说明的背景。详细硬件、缓存和源码设置放在报告条件及链接的记录中。

| 字段 | 内容 |
| --- | --- |
| `title`、`subtitle` | 可选背景或测量条件，不需要时省略或留空。 |
| `updated_at` | 与报告一致的更新时间，包含时区。 |
| `x_label` | `x` 的含义，例如经过的小时数、评测序号或工具调用次数；baseline 位于零点。 |
| `metric` | `name`、`unit` 和改善方向 `direction`；吞吐通常用 `higher`，延迟通常用 `lower`。 |
| `baseline` | 初始配置的原始测量值 `value`、`label` 和证据 `source`。该负载没有可比的初始测量时保留 `value: null`。 |
| `points` | 每个方案的 `x`、`value`、`status` 和 `source`；可选 `label` 添加标注，`checkpoint: true` 选为累计里程碑。 |
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

指标越大越好时，改善倍数为 `value / baseline`；越小越好时为 `baseline / value`。原始 baseline 为 `1×`，倍数越大表示改善越大。局部控制是具名参照，不是新的 `1×` 起点。原始 baseline 缺少匹配测量时，报告保留绝对值，`--progress` 此时显示待基线测量的图。

里程碑图连接 baseline 与标为 `checkpoint: true` 的有效点。每个点代表实测的累计配置；同时保留多项改动时，使用组合后的实测值。后续版本替代早期版本时，保留早期节点以呈现历史。

搜索图展示改善、回退和失败，历史最佳曲线从注明的 baseline 起始。报告保留方案的采用决定。`status` 字段用于控制绘图，普通方案点不需要显示“valid”标签。

恢复任务时沿用同一份数据和 baseline 原点，保留早期已采用版本与失败尝试。后续对照复测用命名 reference 表示，没有新测量的更新保留已有点。
