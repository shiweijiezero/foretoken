# SLO 并发搜索

[English](slo.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，逐步提高客户端并发限额，测量满足服务级别目标（SLO）的最高请求并发峰值，例如要求延迟或吞吐量达到指定值：

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

该命令从并发限额 2 开始搜索，最高到 32，要求请求延迟的 p99 不超过两秒。每个探测点保留相同的 `--num-prompts` 请求预算和请求到达过程。`--num-runs` 控制每点重复次数：SLO 按指标平均值判定，实测并发则取各次运行的最高请求峰值。探测点达标还要求全部请求成功，且所需指标均可取得。

提高限额后，实测同时进行的请求数不再增长时，搜索提前结束；达到配置上限或完成 SLO 失败边界的细化搜索时也会结束。例如，请求预算为 4，在限额 4 和 8 时都测得峰值 4，结果就报告峰值 4、对应限额 4。

支持生成式、多轮、多数据集和 trace 负载。Trace 使用 `--trace-max-concurrency` 设置初始限额，请求到达时间由 trace 时间戳决定。多轮负载的配置限额约束对话数，实测峰值统计请求数；每轮请求都计入请求预算。参数扫描也可在每个参数点分别执行 SLO 搜索。

## 设置条件

`--slo-params` 接受 JSON 数组。同一对象中的条件必须全部满足，不同对象分别进行独立搜索。

| JSON 值 | 搜索结果 |
| --- | --- |
| `[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}]` | 同时满足两个耗时目标的最高实测请求峰值 |
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

控制台与 `slo_results.json` 给出每组条件下的最高达标请求峰值及其配置限额、最后一次实测峰值与限额，以及停止原因。结果对应本次选择的负载和到达率。每个探测点有独立结果目录；W&B 提供搜索汇总，同组探测运行按条件组、并发限额和重复序号命名。

显式部署的快速开始服务不再需要时，用 `foretoken delete examples/quickstart` 删除。
