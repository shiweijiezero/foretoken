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

## 固定请求速率，测量达标率

[DistServe ShareGPT 配置](../../scripts/common/distserve-sharegpt.jsonl)沿用官方实验代码的历史前缀采样和逐请求输出长度。先用被测模型的 tokenizer 准备请求，再将下方 `http://host/v1/completions` 替换为该模型服务的地址：

```bash
python benchmarks/scripts/prepare_distserve_sharegpt.py --model facebook/opt-13b

foretoken perf --url http://host/v1/completions --model facebook/opt-13b \
  --sweep benchmarks/scripts/common/distserve-sharegpt.jsonl \
  --slo-params '[{"ttft":"<=0.25","tpot":"<=0.1"}]' \
  --num-runs 3 --experiment-name distserve-sharegpt --output local,wandb,plot
```

准备脚本自动下载官方实验所用的 ShareGPT 文件。从至少包含三条消息的对话中随机选择历史前缀，用换行符拼接消息正文，并用下一条记录的 token 数作为目标输出长度。保留原代码的短序列筛选规则和输入加输出小于 2048 token 的限制，再以种子 0 抽样 300 个请求。生成的 JSONL 通过 Completions 直接发送 token ID，不添加聊天模板，各请求按记录长度生成输出。准备脚本与被测服务须使用相同 tokenizer；分词器仓库或本地目录与模型名不同时，通过 `--tokenizer` 指定。

扫描沿用官方 OPT-13B DistServe 脚本的 0.75、1.5、3、4.5、6、6.75、7.5、9 请求/秒，使用 Poisson 到达、不限制客户端并发，temperature 为 1，不额外预热。命令将每个速率点重复 3 次，要求 TTFT 不超过 250 毫秒且 TPOT 不超过 100 毫秒。`--slo-params` 只统计指定负载下的达标率，加上 `--slo-search` 才搜索并发。

协议来源：[数据处理](https://github.com/LLMServe/DistServe/blob/main/evaluation/2-benchmark-serving/0-prepare-dataset.py)、[请求抽样与到达过程](https://github.com/LLMServe/DistServe/blob/main/evaluation/2-benchmark-serving/2-benchmark-serving.py)、[速率设置](https://github.com/LLMServe/DistServe/blob/main/evaluation/ae-scripts/e2e/opt-13b-distllm-client.sh)。本命令通过服务的 OpenAI 流式接口计时，原实验代码则读取自定义接口返回的时间戳。

测量模式接受一个条件对象，指标为 `latency`、`ttft`、`tpot` 或 `itl`，单位均为秒。一个请求满足全部条件才算达标；失败或缺少必要指标的请求计为不达标。`itl` 检查每个请求中最大的分片间隔，不是全局 token 间隔的 p99。

扫描结果自动绘制到达率与达标率、请求 goodput、token goodput、延迟的曲线。从结果中读取达到目标达标率（如 90% 或 99%）的最高已测速率，再扩展或细化参数文件中的速率列表以定位边界。各轮先计算达标比例，再汇总均值；不合并所有请求计算一个比例，也不自动搜索容量。

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
