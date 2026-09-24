# SLO 并发搜索

[English](slo.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，搜索满足服务级别目标（SLO）的最大客户端并发，例如要求延迟或吞吐量达到指定值：

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

该命令从并发 2 开始搜索，上限为 32，要求请求延迟的 p99 不超过两秒。每个探测点使用相同的 `--num-prompts` 请求预算；`--num-runs` 控制每点重复次数，并取其指标平均值。

搜索改变客户端并发，保留选定的请求到达过程，支持生成式、多轮和多数据集负载。多轮对话的每次请求计入预算。参数扫描也可在每个参数点分别执行 SLO 搜索。

## 设置条件

`--slo-params` 接受 JSON 数组。同一对象中的条件必须全部满足，不同对象分别进行独立搜索。

| JSON 值 | 搜索结果 |
| --- | --- |
| `[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}]` | 同时满足两个耗时目标的最大并发 |
| `[{"p99_ttft":"<0.05"}, {"p99_tpot":"<0.01"}]` | 分别给出 TTFT 目标和 TPOT 目标的结果 |
| `[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}, {"p99_latency":"<=5"}]` | 一组满足两个平均耗时目标，另一组满足 p99 延迟目标 |

耗时阈值使用秒，支持以下指标：

| 指标 | 名称 |
| --- | --- |
| 请求延迟 | `avg_latency`、`p50_latency`、`p95_latency`、`p99_latency` |
| 首 token 耗时 | `avg_ttft`、`p50_ttft`、`p95_ttft`、`p99_ttft` |
| 每输出 token 耗时 | `avg_tpot`、`p50_tpot`、`p95_tpot`、`p99_tpot` |
| 吞吐量 | `rps`（请求/秒）、`tps`（输出 token/秒） |

## 查看结果

`slo_results.json` 记录各探测点，以及每组条件下满足要求的最大并发。每个探测点有独立结果目录。W&B 中的探测运行共享一个分组，名称包含条件组、并发值和重复序号。

显式部署的快速开始服务不再需要时，用 `foretoken delete examples/quickstart` 删除。
