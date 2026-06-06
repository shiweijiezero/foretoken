# 05 — MTP 投机解码(模块 C1 = 复用;C2 = 自建)

基于 vLLM fork(v0.22.0)+ DeepSeek-V3 / SGLang。C1(MTP 本身)= 复用 vLLM 的原生 MTP,不自建
drafter。C2(goodput 感知的自适应 MTP 控制)= 自建,是未被上游覆盖的空间。

## MTP 概述

把目标模型**自身原生训练的多 token 预测层**改作 drafter(DeepSeek-V3 §2.2;GLM-4.5
`glm4_moe_mtp.py`)。结构上为 EAGLE 风格:`eh_proj(cat[norm(embed next_tok), norm(prev_hidden)])`
→ 一个额外的 transformer 块(一个 MoE 块,复用共享的 embed/head）。DeepSeek/GLM 在
**checkpoint 内部**自带 **`n_predict = num_nextn_predict_layers = 1`** 个 MTP 层。对于一个没有
现成 drafter、也没有事后 EAGLE 头的 200B+ MoE,MTP 是唯一零额外训练的高接受率 drafter。MoE
解码内存受限,每次目标前向接受多个 token 带来显著摊薄。

## vLLM 支持(file:line)

- MTP 走 **EAGLE proposer 路径**:`use_eagle()` 对 mtp 返回 True(`speculative.py:1059`);MTP
  模型类型归一化为 `method="mtp"`(`speculative.py:704`);runner 路由到 `EagleProposer`
  (`gpu_model_runner.py:594`)= 薄的 `SpecDecodeBaseProposer` 子类。
- 配置:`{"method":"mtp","num_speculative_tokens":1}`,**无独立 draft 模型**(权重在目标
  checkpoint 内)。`num_speculative_tokens` 默认为 `n_predict`(=1);必须**能被 n_predict
  整除**;**>1 会循环复用唯一的 MTP 层,导致接受率退化**(`speculative.py:708,766`)。
- **链式(非树)的 propose 循环**(`llm_base_proposer.py:570`);标准 rejection-sampler 验证
  (`rejection_sampler.py`)→ 无损。
- **异步调度:MTP 被放行(gated IN)**(`EagleModelTypes`,`vllm.py:911`)。这是 MTP 相对
  (custom_class)cross_vocab 路径(被 gated OUT)的一个服务优势。要求
  `disable_padded_drafter_batch=false`。
- **CUDA graph:draft 步骤仅 PIECEWISE**(`gpu_model_runner.py:5856`);目标 MoE 可为
  FULL_AND_PIECEWISE。draft 的 batch size 经 `adjust_cudagraph_sizes_for_spec_decode` 捕获。

## 接受率 / 加速

- DeepSeek-V3(论文 §5.4.3):**第 2 个 token 接受率 85–90% → 1.8× TPS。**
- SGLang DeepSeek-V3 16×H200(EP+PD):3 token **接受 2.18**,4 token **2.44,+60.8% 吞吐**;
  **128×H200 2 token 仅 +14.2%**(增益在规模/高并发下缩水);GLM-4.5-Air **1.3–1.8×**(单个
  MTP 层未针对深 draft 充分优化)。
- **区间**:在低/中等 batch + 长上下文下大幅增益;在 MoE 上高 batch + 短上下文时可能转负
  (Cascade arXiv 2506.20675:3 个 draft token → 多达 3× 的专家激活;静态 K 可能慢 1.5×;即便
  K=1 在某些数学任务上也损失 >25%)。MagicDec:长上下文使其重新转正。

## 与 KV + 调度的交互(跨模块约束)

- **Draft KV 是一个独立的 `kv_cache_gid`**(`llm_base_proposer.py:1520`);调度器仅在 decode
  阶段保留 `num_lookahead_tokens` 个额外块(`scheduler.py:705`)。**对 B2/B3 模块的硬约束:把
  drafter 的 `kv_cache_gid` 标记为临时(EPHEMERAL),绝不把 draft KV 提升到卸载层或作为前缀
  缓存**(被拒绝的 draft 写入的 KV 会被丢弃)。当前不是自动的;需要一个显式标记。
- **C2 钩子**:投机长度是一个单一的静态 int(`num_lookahead_tokens`,`scheduler.py:213`),即
  负载自适应控制器的注入点。**约束**:动态 K 必须待在已捕获的 **cudagraph 桶**内,否则 eager
  回退会抹掉增益(最尖锐的风险)。

## C2 = 自建空位(goodput 感知的自适应 MTP 控制)

vLLM 主线尚无已合入的自适应投机长度(仅静态 `num_speculative_tokens` + 静态的
`synthetic_acceptance_rates`)。在途两条 open 路线均未合入且均非 goodput 感知:
`eagle_dynamic` / DynamicProposer(PR #26504,纯接受率阈值)与 DSL 置信度早退(RFC #36657,
纯每步 max-softmax)。C2 的利基因此从"自适应"收窄到 **goodput 感知 + 与 KV/调度跨层耦合 +
MTP-on-EP'd-MoE 专用**。定义此目标的文献 —— SmartSpec(2406.14066,dense)、AdaServe
(2501.12162,SLO 定制)、**Cascade(2506.20675,唯一 MoE 感知,不在 vLLM 中,也非 MTP 专用)**
—— 均不在 vLLM 内。**C2 的差异化定位**:一个 goodput/效用感知的控制器,从观测到的接受率 +
batch/KV 负载 + MoE 专家暂存成本出发,**逐步(per step)**地选取 `num_speculative_tokens`(以及
是否投机),**受桶约束**,为 MTP-on-EP'd-MoE 专门定制。该能力在上游缺失。

## Build-vs-use 与集成计划

- **C1 = 复用** vLLM 原生 MTP。调优项:选 `num_speculative_tokens`(1,在已知接受率会退化的前
  提下扫 1–4);保持 `disable_padded_drafter_batch=false`(异步);开 EP(MTP 验证对 EP 透明);
  **把 draft `kv_cache_gid` 标记为临时**。
- **C2 = 自建**该控制器,基于 `num_lookahead_tokens` 钩子 + 接受率反馈,受桶约束。
- **目标 = GLM-4.5-Air-106B**(`Glm4MoeMTP`,可在 A100 上运行;DeepSeek-V3 在 A100 上不可行 ——
  FP8 KV)。如需更大的 MoE,Qwen3-235B-A22B 有 MTP 变体。
- **阶段 A(C1 基线)**:GLM-4.5-Air + `method=mtp` K∈{1..4},TP+EP;在
  batch{1,4,8,16}×ctx{1k,8k,32k} 上测量接受率/TPS/分阶段,对比 no-spec,得到区间图。
- **阶段 B(C2)**:该控制器;对比静态 K + SmartSpec/Cascade 基线。temp=0 时无损;用 GLM 官方
  采样以贴近真实。

## 不 fork 的 MTP 集成路径(2026-06 最新源码核实)

对照 clone 的最新 vLLM 源码核实(file:line 见 `docs/08`):

- **C1 现成内嵌 MTP,确认可用**:GLM-4.5-Air 自带 MTP 头(config `num_nextn_predict_layers:1`
  + 实际 `model.layers.46` 的 nextn 权重),vLLM `Glm4MoeMTP` 通用支持 `method=mtp`;官方未专门
  给 Air 写 mtp 命令,上线前自测一次确认 nextn 层正确加载。官方背书的替代:
  **Qwen3-Next-80B-A3B**(A100 可运行、recipe 直接给 mtp 命令)。
- **新 MTP 算法(新 proposer 逻辑)→ `custom_class`,不改源码**:`speculative_config.model=
  "你的模块.Proposer"`(实现 `propose()`)→ vLLM 经 `custom_class_proposer.py` 动态加载。
  此前的 cross_vocab 是这条路的现成模板。
- **新草稿头**:`ModelRegistry.register_model` OOT 注册;若全新 `model_type` 不在 `MTPModelTypes`
  Literal 里,改走 `custom_class`(不看 Literal)即可,仍不改源码。
- **唯一需要改源码的**:① 命名化新 `method`(改 `config/speculative.py` Literal +
  `gpu_model_runner.py` 两处 if-elif);② 完全自定义验证(rejection sampler Triton kernel)。一
  般无需。
- **注意**:`custom_class` 在 async scheduling 等 serving 特性上可能不如原生 `method=mtp`
  (旧 fork 记录其被 gated out),实际使用时在最新版核实。

## 开放问题

1. 动态 K 的 cudagraph 重捕获成本(待在桶内 vs eager 回退),最尖锐的风险。
2. GLM 上的 MTP 深度上界(`n_predict=1`):K=2–4 时接受率下降多快?决定 C2 的有用 K 范围。
3. 高 batch 下 EP × draft 专家激活的争用(Cascade 指出其会改变最优 K)。
4. 异步调度 × 动态 K(padded-batch 不变量)。
5. 把 draft-KV 排除在卸载/前缀提升之外(临时标记),显式而非自动。

## 来源

DeepSeek-V3 2412.19437(§5.4.3)· SGLang MTP(lmsys 2025-07-17)· Cascade 2506.20675 · SmartSpec
2406.14066 · AdaServe 2501.12162 · MagicDec 2408.11049 · vLLM MTP 文档 + fork file:line(见上)。
