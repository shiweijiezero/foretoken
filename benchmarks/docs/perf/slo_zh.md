# SLO 并发搜索

[English](slo.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，逐步提高客户端并发限额，测量满足服务级别目标（SLO）的最高请求并发峰值，例如要求延迟或吞吐量达到指定值：

```bash
foretoken perf examples/quickstart \
  --dataset random \
  --min-prompt-length 128 --max-prompt-length 512 \
  --num-prompts 100 --max-concurrency 2 \
  --slo-search --slo-params '[{"p99_latency":"<=2s"}]' \
  --slo-upper-bound 32 \
  --num-runs 1 \
  --output local,wandb
```

该命令从并发限额 2 开始搜索，最高到 32，要求请求延迟的 p99 不超过两秒。每个探测点保留相同的 `--num-prompts` 请求预算和请求到达过程。`--num-runs` 控制每点重复次数：SLO 按指标平均值判定，实测并发则取各次运行的最高请求峰值。探测点达标还要求全部请求成功，且所需指标均可取得。

搜索会在 SLO 边界或配置上限处结束；若提高限额后实测并发不再增长，则提前停止。例如，请求预算为 4，在限额 4 和 8 时都测得峰值 4，结果就报告峰值 4、对应限额 4。

使用 trace 时，`--max-concurrency` 设置在途请求上限，请求按 trace 时间戳到达。多轮负载的限额约束对话数，实测峰值和请求预算则按每轮请求统计。

## 固定对话启动速率，测量达标率

[对话到达率配置](../../scripts/common/conversation-rate.jsonl)扫描每秒启动 2、4、8、16 段对话，数据集通过命令行指定。将下方 URL 和模型名换成服务的 Chat Completions 地址和模型：

```bash
foretoken perf --url http://host/v1/chat/completions --model Qwen/Qwen3-0.6B \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/conversation-rate.jsonl \
  --temperature 0 --random-seed 0 --max-concurrency -1 \
  --num-prompts 100 --warmup-requests 0 \
  --slo-params '[{"ttft":"<=250ms","tpot":"<=100ms"}]' \
  --num-runs 1 --experiment-name sharegpt-rate --output local,wandb,plot
```

到达率控制对话何时启动，SLO 则按各轮 HTTP 请求统计 TTFT 和 TPOT。100 个请求预算按轮次计数，最后一段对话可能在预算耗尽时停止。有文本参考答案的轮次按对应 token 数定长生成，显式长度设置见[对话输出长度](conversations_zh.md)。`--slo-params` 统计这组固定负载的达标情况，`--slo-search` 开启并发搜索。

固定负载测量接受一个条件对象，指标为 `latency`、`ttft`、`tpot` 或 `itl`。耗时阈值使用 `s` 或 `ms` 后缀，例如 `<=2s`、`<=100ms`；不带单位时按秒解释。一个请求满足全部条件才算达标，失败或缺少必要指标的请求计为不达标。`itl` 检查每个请求中最大的分片间隔。

比较各速率下的达标率、goodput（达标请求或 token 的吞吐量）和延迟。选择达到目标达标率（如 90% 或 99%）的最高已测对话启动速率，再扩展或细化速率列表以定位边界。重复运行时，达标率是各轮达标比例的均值，而非合并所有请求后计算的比例。[一秒 SLO 窗口](../metrics_zh.md#slo-结果)展示运行期间达标情况的变化。同时比较阈值与速率，见[阈值扫描](sweep_zh.md#比较-slo-阈值与请求速率)。

## 设置搜索条件

`--slo-params` 接受 JSON 数组。同一对象中的条件必须全部满足，不同对象分别进行独立搜索。

| JSON 值 | 搜索结果 |
| --- | --- |
| `[{"avg_ttft":"<=50ms", "avg_tpot":"<=20ms"}]` | 同时满足两个耗时目标的最高实测请求峰值 |
| `[{"p99_ttft":"<50ms"}, {"p99_tpot":"<10ms"}]` | 分别给出 TTFT 目标和 TPOT 目标的结果 |
| `[{"avg_ttft":"<=50ms", "avg_tpot":"<=20ms"}, {"p99_latency":"<=5s"}]` | 一组满足两个平均耗时目标，另一组满足 p99 延迟目标 |

搜索支持以下指标：

| 指标 | 名称 |
| --- | --- |
| 请求延迟 | `avg_latency`、`p50_latency`、`p95_latency`、`p99_latency` |
| 首 token 耗时 | `avg_ttft`、`p50_ttft`、`p95_ttft`、`p99_ttft` |
| 每输出 token 耗时 | `avg_tpot`、`p50_tpot`、`p95_tpot`、`p99_tpot` |
| 吞吐量 | `rps`（请求/秒）、`tps`（输出 token/秒） |

## 查看结果

控制台与 `slo_results.json` 给出每组条件下的最高达标请求峰值及其配置限额、最后一次实测峰值与限额，以及停止原因。

显式部署的快速开始服务不再需要时，用 `foretoken delete examples/quickstart` 删除。
