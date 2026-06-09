# 投机解码(MTP)

MTP(Multi-Token Prediction)把目标模型**自身原生训练的多 token 预测层**改作 drafter,做投机解码。对没有现成 draft 模型、也没有事后训练 EAGLE 头的大型 MoE,MTP 是零额外训练、高接受率的 drafter——MoE 解码内存受限,每次目标前向接受多个 token 带来显著摊薄。

## 原理

MTP 层在结构上为 EAGLE 风格:拼接「下一 token 的 embedding」与「上一步 hidden」,过一个额外的 transformer 块(复用共享的 embedding / head),预测后续 token。多个模型在 checkpoint 内部自带 MTP 层(如 DeepSeek-V3、GLM-4.5、Qwen3-Next),`n_predict` 通常为 1。验证走标准 rejection sampling,保证无损(接受后的输出分布等于目标模型分布)。

## 配置

参考后端 vLLM 中,MTP 走 EAGLE proposer 路径:

- 配置 `{"method": "mtp", "num_speculative_tokens": K}`,**无独立 draft 模型**(权重在目标 checkpoint 内)。
- `num_speculative_tokens` 默认等于模型的 `n_predict`(常为 1),且须能被其整除;**超过 `n_predict` 会循环复用唯一的 MTP 层,导致接受率退化**。
- 验证为链式(非树)propose 循环 + 标准 rejection sampler,无损。

## 接受率与加速 regime

加速高度依赖负载,不是恒定收益:

- **低 / 中 batch + 长上下文**:加速明显(目标前向内存受限,摊薄收益大)。
- **高 batch + 短上下文**:可能转负——在 MoE 上,多个 draft token 会放大专家激活,静态投机长度可能反而变慢。

因此投机长度应是负载自适应的(按观测接受率与并发动态调整),而非固定值。这正是[路线图](roadmap.md)中「自适应投机解码」的方向。

## 与 KV 和调度的交互

- **draft KV 是临时的**:被拒绝的 draft 写入的 KV 应被丢弃。drafter 的 KV 必须标记为临时(EPHEMERAL),**绝不**提升到卸载层或作为前缀缓存——否则会污染缓存。这是 KV 优化模块必须遵守的硬约束(见[测试与正确性](testing.md)的 EPHEMERAL 契约)。
- **投机长度是调度的一个旋钮**:负载自适应控制器在此注入。约束:动态调整后的投机长度须落在已捕获的 CUDA-graph 桶内,否则 eager 回退会抹掉加速。
- **与 chunked prefill 的兼容性**:部分混合模型的 MTP 配置当前要求关闭 chunked prefill(一个 batch step 既混入 prefill 块、又做多 token 草稿验证时,attention mask / KV 写入 / 调度对不上)。这是实现层约束,评测 MTP 时须相应配置,否则报错或失真。

## 评测

MTP 评测必须用**真实权重 + 真实 / 语义真实文本**——接受率完全取决于真实预测质量与输入语义,占位 token 测出来无意义(对比 KV 评测可用占位 token)。指标:接受率、平均接受长度、TPS 加速比;按负载类型分别报(reasoning / chat / code 的接受率可能不同)。温度为 0 时贪心输出须与关闭 MTP 逐 token 一致(无损校验,见[测试与正确性](testing.md))。
