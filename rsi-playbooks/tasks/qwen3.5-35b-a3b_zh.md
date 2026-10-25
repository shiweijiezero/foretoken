# Qwen3.5-35B-A3B 优化

[English](qwen3.5-35b-a3b.md) | 简体中文

```text
模型：Qwen3.5-35B-A3B
起始配置：examples/rsi/qwen3.5-35b-a3b；BF16 TP4、四步 MTP、128G 本地 CPU KV offload，可在优化中调整
优化目标：提升单并发解码速度和 16 并发总输出吞吐（token/s），降低两组的解码延迟（TPOT）
精度：固定 BF16
部署形态：在最终单机 GPU 预算内自主选择并行方式和各阶段安排
最终部署预算：单机 4 卡；模型与 frontend 工作负载合计不超过 32 核 CPU、250G 主机内存
实验规模：服务候选遵守最终预算；算子、编译或正确性等局部实验按需使用更少资源，包括单卡
可用资源：2 台节点，每台可用 8 张 GPU、64 核 CPU、1 TiB 内存
Advisor 数量：2
Worker 数量：2
Experimenter 数量：2
最小并行实验数：两节点合计 4 组独立实验
最大并行实验数：不固定为 4；按 GPU、CPU、内存和存储的实际需求确定并记录，纳入更小规模的实验
并行安排：先为平台服务、构建和评测客户端预留资源，再按各组资源请求安排四卡候选及更小规模的实验。不足四组时，记录并处理准备或资源方面的具体阻塞
评测负载：每个服务分别测量并发 1 和 16，覆盖随机长度负载和 StudyChat 对话；具体命令见下方“评测命令”
精度评测：针对可能影响精度的改动按需执行 foretoken eval，每完成 20 轮速度迭代最多执行一次
可改动范围：在上述精度与资源约束内，可以修改各层的实现与配置
每轮测量时间预算：perf／eval 测量默认 10 分钟，以加快迭代；Agent 根据实际负载调整测量预算和请求数，记录调整原因与实际耗时
工作时间：从开始执行任务计时，总预算 24 小时
预期／结束条件：持续最大化吞吐、最小化 TPOT，不设固定指标阈值；持续推进诊断、联网调研和优化实验，直至经实测确认无进一步优化空间、工作时间用尽或用户明确要求停止
交付结果（<iteration> 为 --iteration 指定的方案名）：
  - 改动与命令：每轮的代码／配置改动和完整 foretoken perf 命令，写入 results/qwen3.5-35b-a3b/iterations/<iteration>/notes/iteration.md 的“改动与命令”部分。
  - 性能指标：各负载在并发 1／16 下的输出吞吐（token/s）与平均 TPOT（ms/token），写入同一 iteration.md 的“性能指标”表。
  - 对照分析：相同负载下，与初始方案和上一轮的数值对照、变化百分比及原因分析，写入同一 iteration.md 的“对照分析”部分。
  - 结果证据：W&B 链接、图表和原始结果路径，写入同一 iteration.md 的“结果证据”部分；命令自动保存的原始产物保留在 results/qwen3.5-35b-a3b/iterations/<iteration>/runs/ 下。
  - 执行效率：按任务开始至结束的完整时间区间，统计正在执行任务的子 Agent 数的时间加权平均值，并分别统计两台节点的 GPU 平均利用率，写入报告的“执行效率”部分；注明统计时段、资源范围、采样覆盖及低利用率原因，并引用原始记录。
  - 用户报告：按 rsi-playbooks/templates/report-template_zh.md 和实验记录手册中的报告维护流程，创建并持续维护 rsi-playbooks/tasks/qwen3.5-35b-a3b/<experiment>/report.md。包含各负载在并发 1／16 下的对照、相对 base 的变化、保留方案及其代码／配置、结论、执行效率、迭代笔记和 W&B 结果链接；关键图表放在报告旁的 figures/ 中，用相对路径引用。
```

执行与资源安排遵循 [RSI skill](../../.agents/skills/rsi/SKILL_zh.md)，记录方法见[实验记录手册](../experiment-records_zh.md)。

## 起始部署

按目标硬件调整[四卡示例](../../examples/rsi/qwen3.5-35b-a3b/)中的引擎参数和环境变量。准备好[源码版平台](../../docs/deployment/source_zh.md)，由管理员授权[仓库 `data/` 的共享访问](../../docs/features/model-storage_zh.md#跨命名空间复用模型文件)，再从候选源码目录部署：

```bash
foretoken deploy examples/rsi/qwen3.5-35b-a3b --name qwen-rsi -e . --timeout 60m
```

模型与前端的内存请求及上限分别为 `246G` 和 `4G`。模型额度内包含四个 TP worker 合计 `128G` 的 CPU KV offload。最终对照使用可比的四卡服务结果，小型验证用于诊断。

## 评测命令

在仓库根目录执行。下列命令选择起始实例 `qwen-rsi`；对照其他方案时，同时替换候选的部署目录和 `--name`。使用 W&B 前先通过 `wandb login` 登录。结果统一保存到执行机器上的 `results/qwen3.5-35b-a3b/`；`baseline` 是初始方案名，新方案更名，同一方案补测沿用名称。上述笔记由 Agent 填写，指标与图表由命令生成。

下面两段性能评测循环分别按并发 1 和 16 顺序执行，使用相同输入，每组包含 80 条正式请求和 4 条预热请求。最终保留方案必须完成 StudyChat，中间迭代按需执行。GSM8K 命令最多并发发送 64 个 API 请求，由引擎按自身序列数上限调度。更多负载见 [recipes](../../benchmarks/docs/recipes_zh.md)。

```bash
# 每轮速度迭代：随机长度负载，分别测量并发 1 和 16
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b --name qwen-rsi --dataset random \
    --min-prompt-length 128 --max-prompt-length 4096 \
    --min-output-length 128 --max-output-length 512 \
    --max-concurrency "$concurrency" --num-prompts 80 --warmup-requests 4 \
    --random-seed 0 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b --iteration baseline
done

# 最终保留方案必跑：StudyChat 对话，分别测量并发 1 和 16
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b --name qwen-rsi \
    --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
    --max-concurrency "$concurrency" --num-prompts 80 --max-tokens 2048 \
    --warmup-requests 4 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b --iteration baseline
done

# 按需精度评测，最多并发 64 个请求
foretoken eval examples/rsi/qwen3.5-35b-a3b --name qwen-rsi \
  --evaluator lm-eval --tasks gsm8k --num_fewshot 0 --limit 20 \
  --model_args num_concurrent=64 --output experiment,plot,wandb \
  --output-dir results/qwen3.5-35b-a3b --iteration baseline
```
