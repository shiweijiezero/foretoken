<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 蒸馏少步生成模型：一致性训练与分布匹配

[English](few-step-distillation.md) | 简体中文

## 现象与动机

少步扩散通过训练另一个预测器减少网络执行，不是让原 sampler 的 kernel 更快。一致性蒸馏使模型沿去噪轨迹跨大步；因果视频还需解决暴露偏差：推理时后续块依赖学生自己生成的不完美历史，而 teacher-forcing 训练提供干净历史。

## 原理与分析

一致性目标让教师同一轨迹上不同噪声状态的预测指向共同的去噪输出。Guidance 可以蒸馏进学生，因此学生有自己的采样配方。LCM-LoRA 将更新表示为指定基础权重的低秩适配，不是任意模型通用的 adapter。

Causal-rCM 在 teacher-forcing 一致性训练之外加入 self-forcing 分布匹配蒸馏（DMD）。学生用自己的历史展开，冻结教师估计真实分布 score，可训练 fake-score 网络估计学生分布。两者之差提供学生更新方向；fake-score 用学生样本训练。目标是分布接近，而非逐样本与教师相等。

## 设计

因果指南先将双向 Wan 适配成因果教师，进行 teacher-forcing 一致性蒸馏，再用 self-forcing DMD 精修。最后阶段有三类扩散网络：学生、冻结的双向 real-score 教师、可训练 fake-score 网络。推理只保留学生、文本编码器和 VAE。

普通分块推理在每块后还执行一次零噪声的干净上下文计算。保留最后噪声上下文以省掉该 pass，需要匹配 noisy-context 训练和分块设置。通用 rCM 的 dCM→sCM→DMD-with-sCM 配方与这条因果训练路径是不同配置。

## 实现

Self-forcing 训练与推理的核心流程为：

```text
从一致性蒸馏的因果checkpoint初始化学生
冻结real-score教师，初始化可训练fake-score网络
历史, 视频 = 学生展开(prompt, 少步时间表)
x_t = 对视频加噪(采样时间t)
real, fake = 教师score(x_t, prompt), fake-score(x_t, prompt)
根据DMD的score差方向更新学生
用停止学生梯度的样本和去噪损失训练fake-score
推理时仅用学生及保留的历史逐块生成
未启用匹配训练的噪声复用时，刷新干净上下文
流式解码VAE输出，不保留两个score网络
```

rCM 公开 CUDA/PyTorch 训练代码、FSDP2/FlashAttention 配置和 Wan2.1 T2V 1.3B／14B、VAE、umT5 资产入口。引用指南虽列出蒸馏 checkpoint 名称，却没有完整可下载学生清单。需取得兼容的已训练学生或复现训练，仅下载 Wan 教师不够。LCM 则提供已发布的 LCM／LCM-LoRA 系列。

## 影响与效果

直接减少的是学生 transformer 执行次数。总时间仍为 `N_student*T_DiT + T_context + T_text + T_VAE + T_delivery`；去噪变短后，上下文 pass 和解码可能主导。Causal-rCM 指南的每块四步去噪另有一次干净上下文 pass，共五次 transformer，不是四次；两步／一步时间表仍让第一块使用四步，后续块再缩短。匹配训练的 noisy-context 复用才省额外 pass。Self-forcing 让训练覆盖生成历史，缓解干净历史与推理之间的分布差异，代价是训练时的学生展开和两个 score 网络。

分布匹配可以改善真实感，但不保证提示细节或多样性全部保留。长因果展开可能漂移、重复或换主体。BF16/FP16 和 guidance 是训练工作点的一部分；LCM_Dreamshaper_v7 示例使用 FP32，并指出 FP16 可能损失质量。去噪步变少，不等于完整媒体交付按相同比例变快。

## 参考

- [NVIDIA rCM](https://github.com/NVlabs/rcm/tree/ed3cb14dd936f92cdc9f9381af7369991509b41f)：README、资产说明与因果训练入口 的 `ed3cb14`。
- [Causal-rCM 指南](https://github.com/NVlabs/rcm/blob/ed3cb14dd936f92cdc9f9381af7369991509b41f/Causal_rCM.md)：因果训练和推理入口，具体可用性取决于模型与配置。
- [LCM 作者仓库](https://github.com/luosiallen/latent-consistency-model)：训练与公开 LCM/LCM-LoRA 模型说明。
