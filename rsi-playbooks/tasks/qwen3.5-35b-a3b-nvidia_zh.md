# Qwen3.5-35B-A3B NVIDIA A100 80GB GPU 优化

[English](qwen3.5-35b-a3b-nvidia.md) | 简体中文

```text
模型：Qwen3.5-35B-A3B
起始配置：examples/rsi/qwen3.5-35b-a3b-nvidia；BF16 TP2、四步 MTP、36864 上下文，不默认启用 CPU KV offload，可在优化中调整
优化目标：提升单并发解码速度和 16 并发总输出吞吐（token/s），降低两组的解码延迟（TPOT）
精度：固定 BF16
部署形态：在最终单机 GPU 预算内自主选择并行方式和各阶段安排
最终部署预算：单机 2 张 NVIDIA A100 80GB GPU；模型与 frontend 工作负载合计不超过 32 核 CPU、250G 主机内存
实验规模：服务候选遵守最终预算；算子、编译或正确性等局部实验按需使用更少资源，包括单卡
可用资源：实验合计最多使用 6 张 NVIDIA A100 80GB GPU；按实际主机容量安排 CPU、内存和存储
Advisor 数量：2
Worker 数量：2
Experimenter 数量：2
Observer 数量：1
Observer 巡查间隔：1h
最大并行实验数：最多 3 个双卡服务候选；更小规模的局部实验共享六卡预算
并行安排：先为平台服务、构建和评测客户端预留资源，再按各组实际资源请求安排实验，并记录并行度
评测负载：每个服务分别测量并发 1 和 16，覆盖随机长度负载和 StudyChat 对话；具体命令见下方“评测命令”
精度评测：针对可能影响精度的改动按需执行 foretoken eval，每完成 20 轮速度迭代最多执行一次
可改动范围：在上述精度与资源约束内，可以修改各层的实现与配置
每轮测量时间预算：perf／eval 测量默认 10 分钟，以加快迭代；Agent 根据实际负载调整测量预算和请求数，记录调整原因与实际耗时
工作时间：从开始执行任务计时，总预算 24 小时
预期／结束条件：持续提高吞吐、降低 TPOT，不设固定指标阈值。
  持续研究方法、修改实现并做实验。选定的问题已经得到回答，且有依据说明不值得继续投入时，可以结束；时间用尽或用户要求停止时也应结束。
  结束时说明原因，并分别列出测过但没有收益、现有实现跑不了、尚未实现或测试的方向。不能仅因为参数试完了，就认为没有其他优化机会。
交付结果（<iteration> 为 --iteration 指定的方案名）：
  - 改动与命令：每轮的代码／配置改动和完整 foretoken perf 命令，写入 results/qwen3.5-35b-a3b-nvidia/iterations/<iteration>/notes/iteration.md 的“改动与命令”部分。
  - 性能指标：各负载在并发 1／16 下的输出吞吐（token/s）与平均 TPOT（ms/token），写入同一 iteration.md 的“性能指标”表。
  - 对照分析：相同负载下，与初始方案和上一轮的数值对照、变化百分比及原因分析，写入同一 iteration.md 的“对照分析”部分。
  - 结果证据：W&B 链接、图表和原始结果路径，写入同一 iteration.md 的“结果证据”部分；命令自动保存的原始产物保留在 results/qwen3.5-35b-a3b-nvidia/iterations/<iteration>/runs/ 下。
  - 执行效率：按任务开始至结束的完整时间区间，统计正在执行任务的子 Agent 数的时间加权平均值，并分别统计各参与节点的 GPU 平均利用率，写入报告的“执行效率”部分；注明统计时段、资源范围、采样覆盖及低利用率原因，并引用原始记录。
  - 用户报告：按 rsi-playbooks/templates/report-template_zh.md 和实验记录手册中的报告维护流程，创建并持续维护 rsi-playbooks/tasks/qwen3.5-35b-a3b-nvidia/<experiment>/report.md。包含各负载在并发 1／16 下的对照、相对 base 的变化、保留方案及其代码／配置、结论、执行效率、迭代笔记和 W&B 结果链接；关键图表放在报告旁的 figures/ 中，用相对路径引用。
```

执行与资源安排遵循 [RSI skill](../../.agents/skills/rsi/SKILL_zh.md)，记录方法见[实验记录手册](../experiment-records_zh.md)。

## 起始部署

准备好[NVIDIA 源码版平台](../../docs/deployment/source_zh.md)，复用对应硬件的引擎镜像，并授权[仓库 `data/` 的共享访问](../../docs/features/model-storage_zh.md#跨命名空间复用模型文件)。从候选源码目录部署[双卡示例](../../examples/rsi/qwen3.5-35b-a3b-nvidia/)：

```bash
foretoken deploy examples/rsi/qwen3.5-35b-a3b-nvidia --name qwen-rsi-nvidia -e . --timeout 60m
```

模型请求 8 核 CPU、96G 内存，上限为 24 核 CPU、192G 内存；前端请求 1 核 CPU、上限 2 核 CPU，内存请求与上限均为 4G。最终对照使用可比的双卡服务结果，小型实验用于诊断。

## 评测命令

在仓库根目录执行。下列命令选择起始实例 `qwen-rsi-nvidia`；对照其他方案时，同时替换候选的部署目录和 `--name`。使用 W&B 前先通过 `wandb login` 登录。结果统一保存到执行机器上的 `results/qwen3.5-35b-a3b-nvidia/`；`baseline` 是初始方案名，新方案更名，同一方案补测沿用名称。上述笔记由 Agent 填写，指标与图表由命令生成。

下面两段性能评测循环分别按并发 1 和 16 顺序执行，使用相同输入，每组包含 80 条正式请求和 4 条预热请求。最终保留方案必须完成 StudyChat，中间迭代按需执行。GSM8K 命令最多并发发送 64 个 API 请求，由引擎按自身序列数上限调度。更多负载见 [recipes](../../benchmarks/docs/recipes_zh.md)。

```bash
# 每轮速度迭代：随机长度负载，分别测量并发 1 和 16
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b-nvidia --name qwen-rsi-nvidia --dataset random \
    --min-prompt-length 128 --max-prompt-length 4096 \
    --min-output-length 128 --max-output-length 512 \
    --max-concurrency "$concurrency" --num-prompts 80 --warmup-requests 4 \
    --random-seed 0 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b-nvidia --iteration baseline
done

# 最终保留方案必跑：StudyChat 对话，分别测量并发 1 和 16
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b-nvidia --name qwen-rsi-nvidia \
    --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
    --max-concurrency "$concurrency" --num-prompts 80 --max-tokens 2048 \
    --warmup-requests 4 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b-nvidia --iteration baseline
done

# 按需精度评测，最多并发 64 个请求
foretoken eval examples/rsi/qwen3.5-35b-a3b-nvidia --name qwen-rsi-nvidia \
  --evaluator lm-eval --tasks gsm8k --num_fewshot 0 --limit 20 \
  --model_args num_concurrent=64 --output experiment,plot,wandb \
  --output-dir results/qwen3.5-35b-a3b-nvidia --iteration baseline
```
