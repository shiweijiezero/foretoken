<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过批次工作量与解码频率控制提高 SLO 达标率

[English](slo-decode-cadence.md) | 简体中文

## 现象与动机

请求延迟目标不同时，每轮都推进所有 decode，可能把紧缺容量花在可以稍等的流上；总是优先 decode，又可能挤掉等待 prefill 的首 token 机会。批次选择需要同时考虑 TTFT 期限与每个运行请求所需的 token 服务频率。

## 原理与分析

SCORPIO 定义 `TRP = min_target / request_target`，最小 TPOT 目标取自当前运行集合。50 ms 与 100 ms 的目标分别得到 1 和 0.5；每轮累加相应 credit，选中时扣一，宽松请求大约每两轮执行一次。运行集合的最严格目标变化时，重新计算比例。

Virtual batch size 是各比例之和，结合序列长度估计 TPOT。单轮 90 ms 时，credit 算术无法满足 50-ms 目标；平均 TPOT 也不保证每次 token 间隔。

SOLA 按归一化 TTFT/TPOT 压力改善一项延迟、约束另一项。Andes 则比较 token 交付进度与应用消费曲线，让仍有缓冲的流稍等、空缓冲流追上；这要求应用确实允许延后交付。

## 设计

按期限排序 prefill，与差异化 decode 频率和内存可执行性联动。准入与执行共享延迟模型及虚拟批量假设。引擎负责批次成员、频率、prefill 进度和取消；请求层负责原始到达时间、可信目标、入口等待和选实例。路由附带 priority 整数，本身不能保证下一 token 的时间。

## 实现

1. 取得逐请求 TTFT/TPOT 目标，按模型、dtype、backend、拓扑与并行配置校准执行成本。
2. 对候选运行集合计算最严格 TPOT、各 TRP 与虚拟批量，检查预测延迟及内存可执行性。
3. 每轮 decode 为每个请求增加 TRP credit，选择 credit 至少为一的请求，再扣除一个 credit。
4. 按首 token 期限压力排列等待 prefill，在 decode 义务之外安排可执行输入工作。
5. 根据实际批次更新进度和成本估计。SOLA 采用另一种控制方式，按实测 SLO 压力调整阶段顺序与工作量，而非使用 TRP 频率。

引用版本的 vLLM `next_decode_eligible_step` 服务于 V2 异步 pipeline-parallel 的采样 token 广播节奏，不是逐请求 SLO 策略。

## 影响与效果

差异化频率减少宽松流的竞争，为严格生成或等待 prefill 留出容量。跳过轮次会增加间隔波动，并不释放驻留 KV。长度误差影响内存与延迟预测，整体优化也可能牺牲未明确目标的小类别。Goodput 按每秒完成且同时满足两项目标的请求数计量；全到达达标率包含拒绝、超时与取消，不能用已接纳分母隐藏它们。

## 参考

- [SCORPIO，arXiv 2505.23022v2，2026-08-26](https://arxiv.org/html/2505.23022v2)：TRP、virtual batch size 和准入/队列/批次联动。
- [SOLA，MLSys 2025，§§4–5](https://proceedings.mlsys.org/paper_files/paper/2025/file/bc82dbfbfa43232be85b8d9838f49c3e-Paper-Conference.pdf)：有约束的工作量控制与状态排序。
- [Andes，arXiv 2404.16283v1](https://arxiv.org/html/2404.16283v1)：交付曲线体验目标与计入抢占开销的选择。
- [vLLM scheduler，commit 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/sched/scheduler.py)：当前 eligibility、预算与 priority 机制。
