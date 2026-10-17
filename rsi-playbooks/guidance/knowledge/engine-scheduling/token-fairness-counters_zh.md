<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 公平分配 Token 服务：加权计数与缓存局部性额度

[English](token-fairness-counters.md) | 简体中文

## 现象与动机

请求数相等不代表推理服务量相等。长输出接纳后反复占用 decode，仅靠入口顺序公平，无法使它与短请求获得相同服务。Token 计量区分这类成本，局部性感知选择则保留有用的前缀复用。

## 原理与分析

VTC 使用 `service = wp × input_tokens + wq × output_tokens`。输出可赋更高权重以近似 decode 成本，但不是通用 GPU 毫秒换算。权重为 1 和 2 时，1,000 输入／100 输出消耗 1,200 单位，100 输入／1,000 输出消耗 2,100 单位。

VTC 从 counter 最小的等待客户端接纳请求。服务差上界依赖输入与批次有界、容量为正、无抢占等假设，不是逐请求延迟保证。

DLPM 把最长前缀命中与正 deficit 资格结合。扣除未缓存输入后缀和输出，比全量输入更接近实际计算成本，也意味着相同服务份额下，缓存热客户端可得到更多逻辑 token。计量方式决定公平性关注计算、交付 token，还是配额。

## 设计

VTC 按已获得服务量安排准入，执行时持续计费，用 counter lifting 防止闲置期间积攒额外 credit。DLPM 允许缓存局部性较好的连续工作，直到额度耗尽；quantum 越大，局部性越好，短时服务差也越大。D2LPM 增加 client-worker deficit 与缓存候选选择，worker 仍负责本地公平。

引擎拥有真实批次服务，请求层拥有可信身份、公平入口与 dispatch。逐路由 counter 只表达声明的本地范围，不能证明全局引擎服务上界；集群编排负责容量，不再复制公平性状态。

## 实现

VTC 的核心步骤为：

1. 客户端新增第一个等待请求时，将保留 counter 提升到不低于已有等待客户端的最小值；队列此前为空则参考最后离开的客户端。只有运行请求的客户端不参与比较。
2. 有容量时，从等待客户端中选 counter 最小者接纳请求。
3. 输入处理按 `wp` 计费，每个生成 token 按 `wq` 计费；客户端闲置时保留已计服务。

DLPM 仅在客户端 deficit 为正时接纳最长前缀候选。当全部等待客户端 deficit 均不为正时，为 deficit 不为正的活跃客户端补充 quantum，再扣除未缓存后缀和输出成本。

AIBrix `vtc-basic` 在路由时计入预计输入／输出，并按使用量和负载选 pod；引用文件不按实际完成 token 纠正。llm-d 的衰减 LAS 与 turn-priority 属于派发队列，计量契约与引擎算法不同。

## 影响与效果

加权 counter 避免把长输出与廉价请求视为相等服务；deficit 允许缓存局部性，却不允许无限独占，上界取决于请求成本和 quantum。Token 公平准入仍可能留下不均衡的在途 decode，派发公平与执行公平并不相同。Foretoken 已把角色等待、公平派发与实例评分分开，各 owner 保持自己的服务范围。

## 参考

- [VTC，arXiv 2401.00588v2](https://arxiv.org/html/2401.00588v2)：counter lifting、加权服务与证明假设。
- [Locality-aware Fair Scheduling，2501.14312v1，2025-01-24](https://arxiv.org/html/2501.14312v1)：DLPM/D2LPM 与局部性取舍。
- [AIBrix `vtc_basic.go`，commit d0500b03](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/pkg/plugins/gateway/algorithms/vtc/vtc_basic.go)：实际 gateway 计量与选 pod。
- [llm-d program-aware fairness，commit 5253f555](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/flowcontrol/fairness/program-aware/README.md)：现有 dispatch 策略与状态范围。
- [Foretoken 路由](../../../../data-plane/frontend/src/router/README_zh.md)：准入与选实例职责分离。
