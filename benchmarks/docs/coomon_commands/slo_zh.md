# SLO 并发搜索

[English](slo.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，可在延迟或吞吐约束下搜索仍满足 SLO 的最大客户端并发设置。搜索在选定的到达过程下运行，结果不把固定到达率下的客户端并发上限称为服务容量。Foretoken 负责请求预算、搜索和结果发布。

```bash
foretoken perf examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --num-prompts 100 --max-concurrency 2 \
  --slo-params '[{"p99_latency":"<=2"}]' \
  --slo-upper-bound 32 \
  --num-runs 1 \
  --output local,wandb
```

`--max-concurrency` 是并发起点。传入 `--slo-params` 即启用搜索。`--request-rate` 仍表示请求到达过程，因此结果是客户端并发搜索，不代表服务的最大容量。每个探测点都使用相同的 HTTP 请求预算 `--num-prompts`，请求数不会随并发增加。`--num-runs` 会用同一请求预算重复每个探测点并汇总指标。多轮数据集按所有轮次的 HTTP 请求计数；轨迹回放保持选中的轨迹事件和时间戳不变。

## 约束写法

`--slo-params` 接受 JSON 数组，每个元素为一组条件对象：

- 同一对象内的多个指标：AND（必须同时满足）
- 不同对象之间：各自独立二分搜索，分别输出最大并发

整体语义为多组独立搜索：`(组1条件A AND 组1条件B)`、`(组2条件C AND 组2条件D)`、… 各组互不影响。

### AND：同一对象内同时满足

将多个指标写在同一个对象中：

```bash
--slo-params '[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}]'
```

含义：寻找满足 `avg_ttft <= 0.05s` **且** `avg_tpot <= 0.02s` 的最大并发。两个指标都达标，该并发级别才算通过。

### 多组：各组独立搜索

将条件写在不同对象中：

```bash
--slo-params '[{"p99_ttft":"<0.05"}, {"p99_tpot":"<0.01"}]'
```

含义：分别寻找满足 `p99_ttft < 0.05s` 以及满足 `p99_tpot < 0.01s` 的最大并发，各自独立输出结果。

### AND + 多组

```bash
--slo-params '[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}, {"p99_latency":"<=5"}]'
```

- 组 1：`avg_ttft <= 0.05s` AND `avg_tpot <= 0.02s`
- 组 2：`p99_latency <= 5s`

两组各自独立完成二分搜索，分别输出最大并发。

### 指标名

当前可用指标：

- 延迟：`avg_latency`、`p50_latency`、`p95_latency`、`p99_latency`
- TTFT：`avg_ttft`、`p50_ttft`、`p95_ttft`、`p99_ttft`
- TPOT：`avg_tpot`、`p50_tpot`、`p95_tpot`、`p99_tpot`
- 吞吐：`rps`、`tps`

SLO 支持生成式负载、多轮数据集、多数据集和时间戳轨迹回放，搜索配置的客户端 `--max-concurrency` 并保留生成或记录的到达过程。扫描文件可以包含 SLO 条件，每个扫描点分别执行独立搜索。

结果目录含 `slo_results.json`。每个探测点保存在独立目录中；W&B 探测 run 共享同一个 group，名称包含条件组、并发值和重复序号。

使用完后，执行 `foretoken delete examples/quickstart` 删除服务。
