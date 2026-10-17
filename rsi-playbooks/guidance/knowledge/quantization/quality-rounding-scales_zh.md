<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 学习舍入与块缩放，恢复低位模型质量

[English](quality-rounding-scales.md) | 简体中文

## 现象与动机

就近舍入最小化单值即时误差，而不是 transformer block 输出误差。直接校准损失质量时，可离线优化舍入或 scale，在保留既有低位部署格式的同时恢复精度；这与重训全部模型权重不同。

## 原理与分析

参考块 F_l、fake-quantized 块 F̂_l、校准隐状态 h 下，可用 L_block=sum_h ||F̂_l(h;θ)−F_l(h)||²。整数舍入选择相邻级别；FP4 间距不均匀，必须使用真实浮点 fake quantizer。scale/clipping 还改变每个值所在区间。连续 scale 最终要投影为可导出的 FP8，因此导出投影也是数值行为的一部分。

## 设计

AutoRound 捕获块输入/参考输出，优化舍入和 clipping，逐层推进。Scale learning 则用 `amax` 表示 NVFP4 每块权重 scale，经 fake quantization 和直通梯度训练。它支持 scale-only 或权重/scale 联合学习，先用 MSE 初始化，示例采用蒸馏式 KL。顺序重建仅近似跨层作用；scale-only 无法任意移动每个权重。MoE 校准还需真实路由 token 提供专家证据。

## 实现

离线流程为：

1. 捕获校准隐状态与参考块输出。
2. 初始化真实格式的量化器和 scale。
3. 优化 AutoRound 的舍入/clipping 变量 `V`、`α`、`β`，或用直通梯度优化逐块 `amax`。
4. 冻结选择，将 scale 投影到导出表示，量化权重。
5. 沿现有 compressed-tensors/ModelOpt 路径导出，由支持的引擎 kernel 加载。

固定 llm-compressor NVFP4 示例提供逐层 AutoRound 流程。ModelOpt 当前 scale learning 只覆盖 NVFP4 逐块权重 scale，不覆盖全局或激活 scale。每 16 个权重学一个 scale 比联合权重学习少 optimizer state，不代表全部前后向计算减少十六倍。

## 影响与效果

固定 AutoRound 示例报告 Llama-3.1-8B-Instruct NVFP4 的 GSM8K 结果，采用 vLLM/lm_eval、五样例提示、`add_bos_token=true` 和自动 batch。从 0 增至 200 次 AutoRound 迭代，strict-match 准确率由 0.6702 升至 0.6945，但 flexible-extract 由 0.7362 降至 0.7210。Strict-match 与 flexible-extract 对同一任务回答采用不同的答案提取规则，指标同时受答案内容与输出格式影响。

恢复改变离线舍入/scale，不增加在线优化器，代价是校准优化时间和内存；联合训练另增权重及 optimizer state。最终格式不变时可保持同样 kernel 执行而改变质量。降低重建损失无法补救不支持的 kernel、损坏的导出或未观察的专家。

## 参考

- [llm-compressor AutoRound NVFP4 示例](https://github.com/vllm-project/llm-compressor/tree/990fdd2fb41af48f105dbdebd088a96b627701b1/examples/autoround/quantization_w4a4_fp4)。
- [Model Optimizer scale learning 方法与范围](https://nvidia.github.io/Model-Optimizer/announcements/scale-learning.html)。
- [vLLM 部署格式](https://docs.vllm.ai/en/latest/features/quantization/modelopt/)。

另见[MoE 执行](../moe-execution/)。
