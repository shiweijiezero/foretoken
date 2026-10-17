<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少扩散计算：特征复用与求解器目标缓存

[English](diffusion-feature-cache.md) | 简体中文

## 现象与动机

扩散 transformer 会反复处理相近 latent。相邻去噪步中，昂贵的隐藏特征往往变化较慢，但求解器每步仍需新的预测。TeaCache 复用残差，TaylorSeer 预测特征，AutoTarget 选择交给求解器的缓存量。这些方法无需训练新模型，通过近似轨迹减少完整 transformer 执行，不是对原计算做无损加速。

## 原理与分析

TeaCache 计算 timestep 调制输入的相对 L1 变化，用模型特定的拟合多项式估计隐藏计算变化，并累加到上次刷新之后。累计变化小便复用残差。TaylorSeer 用有限差分估计特征导数，按 `F(t+δ) ≈ F(t)+δF′(t)+δ²F″(t)/2` 外推。预测能追踪直接保持旧值所遗漏的缓慢漂移，但突变仍会破坏近似。

求解器接口决定缓存语义。线性流满足 `x_t=(1−t)c+te`、`v=e−c`，锚点可求得 `c=x_a−t_a v_a`、`e=x_a+(1−t_a)v_a`。在 AutoTarget 规定的确定性 Euler 转换条件下，复用速度、干净端点或噪声端点得到相同的锚点间轨迹；复用位移 `h_a v_a` 则不同，新步长为 `h_j` 时误差与 `(h_j−h_a)v_a` 成正比。

## 设计

缓存归属于单次生成，保存锚点特征、差分或残差，以及刷新指标。TeaCache 用阈值控制刷新，TaylorSeer 用阶数和区间控制预测成本。AutoTarget 在相同精确状态上比较合法的求解器目标，以解码后 RGB 误差校准；候选差距过小时放弃选择。它不是 TaylorSeer 的另一名称，而是面向求解器的独立方法。

权重、文本编码器、VAE 和 scheduler 应使用原模型的配套版本，缓存算法不提供替代权重。引用的 vLLM-Omni 集成只在请求级扩散执行使用 cache-dit，step execution 不支持；指南不建议在少步蒸馏模型上使用 TaylorSeer。

## 实现

残差复用的核心过程可写为：

```text
残差, 上次输入, 累计变化 = 空, 空, 0
遍历求解器时间表中的 t:
    h = patch_embed(x); u = 时间步调制(h, t, 条件)
    若上次输入存在: 累计变化 += 校准后的相对变化(u, 上次输入)
    若残差为空或达到刷新条件(t, 累计变化):
        残差 = transformer_blocks(h, t, 条件) - h
        累计变化 = 0
    预测 = 投影并还原latent(h + 残差, t)
    x, 上次输入 = 求解器更新(预测, t, x), u
返回 VAE.decode(x)
```

边界步刷新和变化校准遵循具体模型实现。TaylorSeer 将保持残差替换为有限差分外推。缓存保持 pipeline 既定精度；跳过 transformer 时仍有缓存存储、归约和预测开销。

## 影响与效果

TeaCache v1 在单张 A800 80GB 上，以 OpenSora-Plan 生成 65 帧、512² 视频，采样 150 步：阈值 0.1 时延迟由 99.65 s 降至 22.62 s，快 4.41 倍，VBench 从 80.39% 变为 80.32%。阈值 0.2 时为 14.60 s、快 6.83 倍，但 VBench 降至 79.72%，体现更积极复用的质量代价。

TaylorSeer v1 用 H800、FLUX.1-dev、200 个 DrawBench 提示、1000×1000 输出和 50 步对照，区间 6／一阶预测取得 3.53 倍延迟加速，Image Reward 为 0.9953，对照为 0.9898。4.99 倍 FLOPs 减少不是延迟加速倍数。VAE、文本编码和交付不随 transformer 一起消失；缓存还增加内存，并可能影响提示遵循、细节、运动及多样性。

## 参考

- [TeaCache，arXiv:2411.19108v1](https://arxiv.org/html/2411.19108v1) 与[作者代码](https://github.com/ali-vilab/TeaCache)：残差与累积代理机制。
- [TaylorSeer，arXiv:2503.06923v1](https://arxiv.org/html/2503.06923v1) 与[作者代码](https://github.com/Shenyi-Z/TaylorSeer)：预测递推及模型入口。
- [AutoTarget，arXiv:2610.03577v1](https://arxiv.org/html/2610.03577v1)：2026-10-02 提交，方法；[公开实现](https://github.com/wali1024-offical/AutoTarget/tree/e91a428b8bbc37a6661576a182e39e94e3cc55dd)版本为 2026-10-01 的 `e91a428`。
- [Omni 执行模式](https://docs.vllm.ai/projects/vllm-omni/en/latest/user_guide/diffusion/execution_modes/)与 [cache-dit 文档](https://github.com/vllm-project/vllm-omni/blob/37f094a388ba1c79f83c129fda39797c1ebfc2a4/docs/user_guide/diffusion/cache_acceleration/cache_dit.md)：源码路径版本为 `37f094a`；`v0.30.0` 发布于 2026-09-25。
