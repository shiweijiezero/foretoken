# SLA 自动调参

[English](sla.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，可在延迟或吞吐约束下搜索仍满足 SLA 的最大并发。搜索与 `--sla-params` 解析复用 EvalScope；Foretoken 负责结果发布。仅调闭式 `--parallel`，不调到达率。

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --parallel 2 \
  --sla-params '[{"p99_latency":"<=2"}]' \
  --sla-upper-bound 32 \
  --num-runs 1 \
  --output local,wandb
```

`--parallel` 是搜索起点。传入 `--sla-params` 即启用搜索。每个探测点的请求数为 `number = round(parallel * 倍数)`（默认倍数 2），因此 SLA 搜索期间普通的 `--number` 不是探测预算。`--num-runs` 表示每个并发探测点平均的运行次数。请省略 `--rate` 或保持 `--rate -1`。

## 约束写法

`--sla-params` 接受 JSON 数组，每个元素为一组条件对象：

- 同一对象内的多个指标：AND（必须同时满足）
- 不同对象之间：各自独立二分搜索，分别输出最大并发

整体语义为多组独立搜索：`(组1条件A AND 组1条件B)`、`(组2条件C AND 组2条件D)`、… 各组互不影响。

### AND：同一对象内同时满足

将多个指标写在同一个对象中：

```bash
--sla-params '[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}]'
```

含义：寻找满足 `avg_ttft <= 0.05s` **且** `avg_tpot <= 0.02s` 的最大并发。两个指标都达标，该并发级别才算通过。

### 多组：各组独立搜索

将条件写在不同对象中：

```bash
--sla-params '[{"p99_ttft":"<0.05"}, {"p99_tpot":"<0.01"}]'
```

含义：分别寻找满足 `p99_ttft < 0.05s` 以及满足 `p99_tpot < 0.01s` 的最大并发，各自独立输出结果。

### AND + 多组

```bash
--sla-params '[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}, {"p99_latency":"<=5"}]'
```

- 组 1：`avg_ttft <= 0.05s` AND `avg_tpot <= 0.02s`
- 组 2：`p99_latency <= 5s`

两组各自独立完成二分搜索，分别输出最大并发。

### 指标名

当前可用指标：

- 延迟：`avg_latency`、`p50_latency`、`p95_latency`、`p99_latency`
- TTFT：`avg_ttft`、`p50_ttft`、`p90_ttft`、`p95_ttft`、`p99_ttft`
- TPOT：`avg_tpot`、`p50_tpot`、`p90_tpot`、`p95_tpot`、`p99_tpot`
- 吞吐：`rps`、`tps`

SLA 不能与 `--trace`、`--sweep`、`--parallel -1`、正的 `--rate` 或多 `--dataset` 组合。

结果目录含 `sla_results.json`，以及带 `sla` 字段的 `metrics.json`。

使用完后，执行 `foretoken delete examples/quickstart` 删除服务。
