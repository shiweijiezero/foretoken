# SLO 并发搜索

[English](slo.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，逐步提高客户端并发限额，测量满足服务级别目标（SLO）的最高请求并发峰值，例如要求延迟或吞吐量达到指定值：

```bash
foretoken perf examples/quickstart \
  --dataset random \
  --min-prompt-length 128 --max-prompt-length 512 \
  --num-prompts 100 --max-concurrency 2 \
  --slo-search --slo-params '[{"p99_latency":"<=2"}]' \
  --slo-upper-bound 32 \
  --num-runs 1 \
  --output local,wandb
```

该命令从并发限额 2 开始搜索，最高到 32，要求请求延迟的 p99 不超过两秒。每个探测点保留相同的 `--num-prompts` 请求预算和请求到达过程。`--num-runs` 控制每点重复次数：SLO 按指标平均值判定，实测并发则取各次运行的最高请求峰值。探测点达标还要求全部请求成功，且所需指标均可取得。

搜索会在 SLO 边界或配置上限处结束；若提高限额后实测并发不再增长，则提前停止。例如，请求预算为 4，在限额 4 和 8 时都测得峰值 4，结果就报告峰值 4、对应限额 4。

使用 trace 时，`--max-concurrency` 设置在途请求上限，请求按 trace 时间戳到达。多轮负载的限额约束对话数，实测峰值和请求预算则按每轮请求统计。

## 固定对话启动速率，测量达标率

[ShareGPT 到达率配置](../../scripts/common/sharegpt-rate.jsonl)自动下载原始对话数据，扫描每秒启动 2、4、8、16 段对话的起始负载。将下方 URL 和模型名换成服务的 Chat Completions 地址和模型：

```bash
foretoken perf --url http://host/v1/chat/completions --model Qwen/Qwen3-0.6B \
  --sweep benchmarks/scripts/common/sharegpt-rate.jsonl \
  --num-prompts 300 --warmup-requests 0 \
  --slo-params '[{"ttft":"<=0.25","tpot":"<=0.1"}]' \
  --num-runs 3 --experiment-name sharegpt-rate --output local,wandb,plot
```

数据集按记录的顺序执行完整多轮对话。300 个请求的预算按各轮 HTTP 请求计数，最后一段对话可能在用尽预算时停止。`max_tokens: 4096` 是每轮生成上限，不是固定输出长度。到达率控制对话何时启动，SLO 则逐条 HTTP 请求统计 TTFT 和 TPOT。上述速率是 Foretoken 的起始负载，不是某篇论文的模型专属参数。只有 `--slo-search` 才搜索并发。

[DistServe 的服务评测代码](https://github.com/LLMServe/DistServe/blob/main/evaluation/2-benchmark-serving/2-benchmark-serving.py)可作为请求到达与 SLO 实验的参考；本配置不宣称复现其数据准备和模型专属设置。

测量模式接受一个条件对象，指标为 `latency`、`ttft`、`tpot` 或 `itl`，单位均为秒。一个请求满足全部条件才算达标；失败或缺少必要指标的请求计为不达标。`itl` 检查每个请求中最大的分片间隔，不是全局 token 间隔的 p99。

扫描结果自动绘制到达率与达标率、请求 goodput、token goodput、延迟的曲线。各次运行也会沿吞吐和延迟的时间轴绘制[一秒 SLO 窗口](../../metrics_zh.md#slo-结果)。一次比较多个 SLO 阈值和速率，可使用[阈值扫描](sweep_zh.md#比较-slo-阈值与请求速率)。从结果中读取请求达标率达到目标（如 90% 或 99%）的最高已测对话启动速率，再扩展或细化参数文件中的速率列表以定位边界。各轮先计算达标比例，再汇总均值；不合并所有请求计算一个比例，也不自动搜索容量。

## 设置搜索条件

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

控制台与 `slo_results.json` 给出每组条件下的最高达标请求峰值及其配置限额、最后一次实测峰值与限额，以及停止原因。

显式部署的快速开始服务不再需要时，用 `foretoken delete examples/quickstart` 删除。
