# Qwen 吞吐与解码延迟优化

[English](qwen-decode.md) | 简体中文

```text
模型：Qwen3.5-35B-A3B
起始配置：examples/rsi/qwen-decode；可修改的 BF16 TP4 起点，不固定后续优化策略
优化目标：提升单并发解码速度和 16 并发总输出吞吐（token/s），降低两组的解码延迟（TPOT）
精度：固定 BF16
部署形态：在最终单机 GPU 预算内自主选择并行方式和各阶段安排
最终部署预算：单机 4 卡；模型与 frontend 工作负载合计不超过 32 核 CPU、512 GiB 内存
实验规模：服务候选遵守最终预算；算子、编译或正确性等局部实验按需使用更少资源，包括单卡
可用资源：2 台节点，每台可用 8 卡 MetaX C500、64 核 CPU、1 TiB 内存
最小并行工作子 Agent 数：6
最大并行工作子 Agent 数：20
最小并行实验数：两节点合计 4 组独立实验
最大并行实验数：不固定为 4；按 GPU、CPU、内存和存储的实际需求确定并记录，纳入更小规模的实验
并行安排：每节点安排两组四卡候选，或更多小规模实验。不足四组时，记录并处理准备或资源方面的具体阻塞
评测负载：每个服务分别测量并发 1 和 16，覆盖随机长度负载和 StudyChat 对话；具体命令见下方“评测命令”
精度评测：针对可能影响精度的改动按需执行 foretoken eval，每完成 20 轮速度迭代最多执行一次
可改动范围：在上述精度与资源约束内，可以修改各层的实现与配置
每轮测量时间预算：perf／eval 测量默认 10 分钟，以加快迭代；Agent 根据实际负载调整测量预算和请求数，记录调整原因与实际耗时
工作时间：从开始执行任务计时，总预算 24 小时
预期／结束条件：持续最大化吞吐、最小化 TPOT，不设固定指标阈值；持续推进诊断、联网调研和优化实验，直至经实测确认无进一步优化空间、工作时间用尽或用户明确要求停止
交付结果（<iteration> 为 --iteration 指定的方案名）：
  - 改动与命令：每轮的代码／配置改动和完整 foretoken perf 命令，写入 results/qwen-decode/iterations/<iteration>/notes/iteration.md 的“改动与命令”部分。
  - 性能指标：各负载在并发 1／16 下的输出吞吐（token/s）与平均 TPOT（ms/token），写入同一 iteration.md 的“性能指标”表。
  - 对照分析：相同负载下，与初始方案和上一轮的数值对照、变化百分比及原因分析，写入同一 iteration.md 的“对照分析”部分。
  - 结果证据：W&B 链接、图表和原始结果路径，写入同一 iteration.md 的“结果证据”部分；命令自动保存的原始产物保留在 results/qwen-decode/iterations/<iteration>/runs/ 下。
  - 执行效率：按任务开始至结束的完整时间区间，统计正在执行任务的子 Agent 数的时间加权平均值，并分别统计两台节点的 GPU 平均利用率，写入 results/qwen-decode/notes/experiment.md 的“执行效率”部分；注明统计时段、资源范围、采样覆盖及低利用率原因，并引用原始记录。
  - 最终方案：保留方案、对应代码／配置、各负载实测结果及逐轮对照汇总，写入 results/qwen-decode/notes/experiment.md。
  - 用户汇总：按实验名创建 rsi-playbooks/tasks/qwen-decode/<experiment>/，按 rsi-playbooks/templates/report-template_zh.md 为未参与实验的读者编写该目录的 report.md，展示逐轮改动、指标实测值及相对 base 的变化、结论、执行效率和 W&B 结果链接，关键图表放入 figures/ 并用相对路径引用；交付汇总文件路径。
```

执行与资源安排遵循 [RSI skill](../../.agents/skills/rsi/SKILL_zh.md)，记录方法见[实验记录手册](../experiment-records_zh.md)。

## 起始部署

[四卡起始配置](../../examples/rsi/qwen-decode/)需要已配置兼容运行环境的沐曦 Foretoken 平台、Gateway 前端访问，以及支持 `ReadWriteOnce` 的默认 StorageClass。执行 `foretoken deploy examples/rsi/qwen-decode --timeout 60m` 部署，再在任务预算内演进。服务收益采用可比的四卡结果判断，小型验证用于回答局部问题，不代替最终服务评测。交付前在没有竞争实验负载的条件下验证保留方案。

## 评测命令

在仓库根目录执行。下列命令使用起始部署，对照其他方案时换成各候选自己的部署目录。使用 W&B 前先通过 `wandb login` 登录。结果统一保存到执行机器上的 `results/qwen-decode/`；`baseline` 是初始方案名，新方案更名，同一方案补测沿用名称。上述笔记由 Agent 填写，指标与图表由命令生成。

随机负载需分别用 `--max-concurrency 1` 和 `16` 运行两次；两次运行的请求数、预热量和评测样本量保持一致。最终保留方案还需用 StudyChat 分别测量并发 1 和 16，每组 128 条正式请求，使用相同输入和预热量；中间迭代按需执行。更多负载见 [recipes](../../benchmarks/docs/recipes_zh.md)。

```bash
# 每轮速度迭代：不同输入输出长度
foretoken perf examples/rsi/qwen-decode --dataset random \
  --min-prompt-length 128 --max-prompt-length 4096 \
  --min-output-length 128 --max-output-length 512 \
  --max-concurrency 16 --num-prompts 160 --warmup-requests 4 \
  --random-seed 0 --temperature 0 --output experiment,raw,plot,wandb \
  --output-dir results/qwen-decode --iteration baseline

# 最终保留方案必跑，中间按需：StudyChat 对话
foretoken perf examples/rsi/qwen-decode \
  --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
  --max-concurrency 16 --num-prompts 128 --max-tokens 2048 \
  --warmup-requests 4 --temperature 0 --output experiment,raw,plot,wandb \
  --output-dir results/qwen-decode --iteration baseline

# 按需精度评测
foretoken eval examples/rsi/qwen-decode \
  --tasks gsm8k --num_fewshot 0 --limit 20 --output experiment,plot,wandb \
  --output-dir results/qwen-decode --iteration baseline
```
