# Qwen 解码速度优化

[English](qwen-decode.md) | 简体中文

提高 Qwen3.5-35B-A3B BF16 的单请求解码速度，同时保持回答质量。优化范围不限定组件，可以从推理引擎、调度、算子、通信或服务配置入手，由测量决定探索方向。

## 准备比较条件

准备可运行的 Qwen3.5-35B-A3B Kustomize 配置，部署方法见[源码部署](../../docs/custom-deployment_zh.md)。使用 BF16 权重，记录 GPU、并行方式、模型版本、思考模式和生成参数，并在比较时保持一致。若某项条件本身就是优化对象，明确说明其变化。

将下面的 `MODEL_CONFIG` 改为该配置的实际目录。`baseline` 表示改动前的参考测量，后续用方案名称区分迭代。

```bash
MODEL_CONFIG=path/to/qwen35-deployment
EXPERIMENT=results/decode-speed/qwen35-bf16
ITERATION=baseline

foretoken perf "$MODEL_CONFIG" --dataset random \
  --min-prompt-length 128 --max-prompt-length 128 \
  --min-output-length 512 --max-output-length 512 \
  --max-concurrency 1 --num-prompts 8 --warmup-requests 2 \
  --random-seed 0 --temperature 0 \
  --output experiment --output-dir "$EXPERIMENT" \
  --iteration "$ITERATION"
```

该负载使用 128 token 输入、512 token 输出和单请求并发，用于测量解码性能。[定长输出](../../benchmarks/docs/perf/random_zh.md)要求服务支持 `min_tokens`、`ignore_eos` 并返回输出用量。输入长度应按目标场景调整；同一组比较使用相同负载。

## 分析与设计

先看每输出 token 耗时（TPOT）、请求延迟、实际输出长度和成功数。需要定位瓶颈时，另跑一次[性能剖析](../../benchmarks/docs/profile/README_zh.md)，观察计算、通信和等待分别占用多少时间。剖析会增加开销，速度比较使用不带剖析的结果。

根据证据提出假设，例如减少某段通信等待是否能降低 TPOT。设计具体改动和验证方法，不预先限定必须改引擎或路由。修改后重新部署，将 `ITERATION` 改为方案名称，再执行上面的同一负载。

## 验证回答质量

随机定长负载用于速度比较，回答质量另用真实任务验证。保持模型和思考设置一致，对改动前后分别运行 GSM8K：

```bash
foretoken eval "$MODEL_CONFIG" --tasks gsm8k --limit 20 --log_samples \
  --output experiment --output-dir "$EXPERIMENT" \
  --iteration "$ITERATION"
```

根据模型的思考开销设置足够的生成预算，参数见[质量评测](../../benchmarks/docs/eval/README_zh.md)。检查最终回答和结束原因，区分预算耗尽、答案格式不符与答案错误。少量样本用于快速发现问题，需要更强结论时再扩大评测。

## 每次运行后

在 `$EXPERIMENT/iterations/$ITERATION/runs/` 中找到本次运行，按[结果查看顺序](../templates/experiments_zh.md#查看本次结果)检查状态、配置、指标和原始输出。

由用户或 Agent 编辑该迭代的 `notes/iteration.md`，写明本轮假设、实际改动、TPOT 对比、质量结果和结论，并链接运行证据。若速度变化伴随输出变短或思考模式变化，单独解释，不能直接归因为实现优化。

本轮结束后更新 `$EXPERIMENT/notes/experiment.md`，决定保留、调整或回退改动。仍无法解释差异时，明确下一次需要补充的测量。详细记录和资源收尾见[实验记录](../templates/experiments_zh.md#每轮结束后)。
