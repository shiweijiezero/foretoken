# Qwen3.5-35B-A3B 沐曦优化

[English](qwen3.5-35b-a3b-muxi.md) | 简体中文

```text
模型：Qwen3.5-35B-A3B
起始配置：examples/rsi/qwen3.5-35b-a3b；双卡张量并行（TP2）、四步多 token 预测（MTP）、36,864 上下文，KV 缓存不卸载到主机内存，可在优化中调整
优化目标：提高并发 1 和 16 的输出吞吐（token/s），降低解码阶段每个输出 token 的耗时（TPOT）
精度：固定 BF16
部署形态：在最终单机 GPU 预算内自主选择并行方式和各阶段安排
最终部署预算：单机 2 张沐曦 GPU；模型与 frontend 工作负载合计不超过 32 核 CPU、250G 主机内存
实验规模：服务候选遵守最终预算；算子、编译或正确性等局部实验按需使用更少资源，包括单卡
可用资源：实验合计最多使用 6 张沐曦 GPU；按实际主机容量安排 CPU、内存和存储
Explorer 数量：2
Worker 数量：4
Experimenter 数量：3
Observer 数量：1
Observer 巡查间隔：1h
最大并行实验数：最多 3 个双卡服务候选；更小规模的局部实验共享六卡预算
并行安排：先为平台服务、构建和评测客户端预留资源，再按各组实际资源请求安排实验，并记录并行度
评测负载：每个服务分别测量并发 1 和 16，覆盖随机长度负载和 StudyChat 对话；具体命令见下方“评测命令”
精度评测：针对可能影响回答质量的改动按需执行 foretoken eval，优先评测准备保留的候选方案；已有结果的适用条件未变时复用结果，避免每次性能测量后都重复评分。
可改动范围：在上述精度与资源约束内，可以修改各层的实现与配置
每轮测量时间预算：perf／eval 测量默认 10 分钟，以加快迭代；Agent 根据实际负载调整测量预算和请求数，记录调整原因与实际耗时
工作时间：从开始执行任务计时，总预算 24 小时
预期／结束条件：持续提高吞吐、降低 TPOT，不设固定指标阈值。
  持续研究、实现和验证，包括参数调整与源码改进。研究问题已有答案且进一步投入价值有限时，可以结束；时间用尽或用户要求停止时结束。
  结束时说明依据，分别列出已测但无收益、未能运行的实现、尚未实现或测试的方向。
交付结果（<iteration> 为 --iteration 指定的方案名）：
  - 迭代记录：在 results/qwen3.5-35b-a3b-muxi/iterations/<iteration>/notes/iteration.md 记录动机、代码与配置改动、完整命令、结果和下一步。
  - 性能对照：在迭代记录中按负载列出并发 1／16 的输出吞吐（token/s）和平均 TPOT（ms/token），与初始方案和上一轮对照，说明变化百分比及原因。
  - 结果证据：命令产物保存在同一迭代目录的 runs/ 下；在笔记中链接原始结果、图表和 W&B。
  - 执行效率：统计任务全程的活跃子 Agent 数时间加权均值及各节点 GPU 平均利用率，注明时段、资源范围、采样覆盖和低利用率原因，附原始记录。
  - 用户报告：持续更新 rsi-playbooks/tasks/qwen3.5-35b-a3b-muxi/<experiment>/report.md，汇总各负载对照、相对 baseline 的变化、保留方案及代码／配置、结论和执行效率，并链接迭代记录与 W&B。关键图表放在报告旁的 figures/ 中，用相对路径引用。
```

执行与资源安排遵循 [RSI skill](../../.agents/skills/rsi/SKILL_zh.md)。按[实验记录手册](../experiment-records_zh.md)保存过程与结果，按[报告模板](../templates/report-template_zh.md)组织报告。

## 起始部署

使用已有的沐曦源码平台，缺少时按[沐曦平台指南](../../docs/deployment/metax-platform_zh.md#从源码安装)准备。优先复用兼容镜像和[共享模型文件](../../docs/features/model-storage_zh.md)。从候选源码目录部署[双卡示例](../../examples/rsi/qwen3.5-35b-a3b/)：

```bash
foretoken deploy examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi -e . --timeout 60m
```

命令等待模型和前端就绪后返回。两者合计请求 2 张 GPU、9 核 CPU、100G 内存，CPU 和内存上限合计为 26 核、196G。

## 评测命令

在仓库根目录执行，使用 W&B 前先运行 `wandb login`。结果保存到 `results/qwen3.5-35b-a3b-muxi/`，初始方案名为 `baseline`；新方案使用新名称，补测沿用原方案名。对照其他候选时，同时替换部署目录、`--name` 和 `--iteration`。

两个性能评测循环依次测量并发 1 和 16，使用相同输入：随机负载每组 80 条正式请求，StudyChat 每组 200 条，每组另加 4 条预热请求。StudyChat 按对话中的各轮请求计数。最终保留方案需完成 StudyChat，中间迭代按需执行。其他负载见[评测示例](../../benchmarks/docs/recipes_zh.md)。

GSM8K 最多并发 64 个请求。输出预算包含思考内容，由 Foretoken 限制为部署上下文上限减去套用聊天模板后的输入长度；起始部署下即 `36864 - input_tokens`。命令以 [Qwen3.5 的原生总上下文大小](https://huggingface.co/Qwen/Qwen3.5-35B-A3B#model-overview)为请求上界，设置 `max_gen_toks=262144`。

```bash
# 每轮速度迭代：随机长度负载，分别测量并发 1 和 16
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi --dataset random \
    --min-prompt-length 128 --max-prompt-length 4096 \
    --min-output-length 128 --max-output-length 512 \
    --max-concurrency "$concurrency" --num-prompts 80 --warmup-requests 4 \
    --random-seed 0 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b-muxi --iteration baseline
done

# 最终保留方案必跑：StudyChat 对话，分别测量并发 1 和 16
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi \
    --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
    --max-concurrency "$concurrency" --num-prompts 200 --max-tokens 2048 \
    --warmup-requests 4 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b-muxi --iteration baseline
done

# 按需精度评测，最多并发 64 个请求
foretoken eval examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi \
  --evaluator lm-eval --tasks gsm8k --num_fewshot 0 --limit 20 \
  --model_args num_concurrent=64,max_gen_toks=262144 --output experiment,plot,wandb \
  --output-dir results/qwen3.5-35b-a3b-muxi --iteration baseline
```
