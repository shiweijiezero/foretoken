# 按层组推进 MoE 预填充

[English](layered-prefill.md) | 简体中文

在预填充与解码共置的 MoE 服务中，按连续层组推进预填充，可以减少小 token 分块反复读取专家权重的开销，同时给已有请求保留解码机会。

## 思路

常见的 chunked prefill 沿 token 维度切分 prompt。每轮只处理一段 tokens，但这段输入仍经过所有模型层。对细粒度 MoE，较小的分块就可能激活大部分专家，每个专家却只处理少量 tokens；后续分块经过同一层时又要读取这些专家权重。

Layered prefill 改变调度维度：一轮让完整 prompt 经过一组连续层，然后保存中间状态，把剩余层组留到后续轮次。已有 decode 请求每轮仍经过全部层。

```text
按 token 分块：第 1 段 tokens → 全部层；第 2 段 tokens → 全部层
按层组推进：  完整 prompt → 第 1 组层；完整 prompt → 第 2 组层
```

在某个 MoE 层内一次处理更多 prompt tokens，可让同一批专家权重服务更多 tokens，减少分块造成的重复加载。超长 prompt 仍可切成较大的 token chunks，再在每个 chunk 内按层组推进。

## 适用场景

- 预填充与解码共置，有长 prompt 持续进入，同时需要控制已有请求的 token 间隔。
- MoE 的小 token chunks 激活了较多专家，专家权重读取成为明显开销；decode batch 较小时更值得调查。
- 可以改动引擎调度和模型执行，而不只是调整服务层路由。

按层暂停和恢复的思路不绑定一种 GPU，但论文收益依赖 MoE 专家访问模式。作者在 dense Qwen3-8B 上观察到退化；放宽 token 间隔要求、允许更大的 token chunk 后，两种调度的差距也会缩小。只有单个请求、prefill 已完成且没有新 prefill 插入时，这个机制没有额外的 prefill 权重读取可以消除。

## 实现线索

[公开实现](https://github.com/scale-snu/layered-prefill/tree/053f80e5201a7c0ab56e468e3d578907a2ca9cc3)基于 Nano-vLLM，提供层组调度及基准命令。

核对日期为 2026-10-07。在 vLLM `1be36283678a9a94fc8fdaad6c95c2896d6b4015` 的[调度输出](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/vllm/v1/core/sched/output.py)中，工作量按每请求 token 数表达；[Qwen3 MoE 执行路径](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/vllm/model_executor/models/qwen3_moe.py)遍历当前流水线阶段所属层。已审阅的路径没有跨轮保存请求层组进度的接口，不能通过修改 token budget 直接实现该机制。

迁移时需要设计以下执行状态：

1. 调度器为 prefill 请求记录当前层组和 prompt/chunk 范围，区分“本组完成”与“全部 prefill 完成”。
2. Runner 和模型 forward 支持执行指定层组，并跨轮保存 hidden states、所需 residual、位置与 KV 映射。只有最后一组完成后才能产出首 token。
3. 重新处理抢占、取消、批次重组和 CUDA Graph 的状态与形状约束。中间状态会占用显存，层组过大也可能拉长 decode 等待。

先用相同请求轨迹比较 token 分块与层组调度，观察 TTFT、token 间隔尾延迟、显存峰值及专家权重读取。对比时保持模型和采样条件一致，并检查输出一致性。在混合服务负载下测量端到端收益。

## 参考资料

- [From Tokens to Layers: Redefining Stall-Free Scheduling for MoE Serving with Layered Prefill，v2](https://arxiv.org/html/2510.08055v2)：主实验使用 2 张 H100 80GB、NVLink、TP2，测试 Qwen3-30B-A3B 和 GPT-OSS-20B 的 ShareGPT/arXiv 混合服务负载。作者按专家权重加载字节数统计，报告两类负载分别下降 12% 和 39%。
- [Layered Prefill 实现与复现说明](https://github.com/scale-snu/layered-prefill/tree/053f80e5201a7c0ab56e468e3d578907a2ca9cc3)：包含依赖、FlashAttention 补丁、服务与测量命令。
