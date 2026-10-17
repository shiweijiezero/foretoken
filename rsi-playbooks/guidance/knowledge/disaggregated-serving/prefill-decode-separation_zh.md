<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少预填充与解码干扰：按缓存和负载选择阶段分离

[English](prefill-decode-separation.md) | 简体中文

## 现象与动机

长 prefill 会拖慢共享引擎的全部 decode。P/D 分离消除干扰，也让批处理和并行分别配置，但增加队列、KV 传输、重复权重。条件分离按实际未缓存工作与 decode 余量判断隔离收益是否超过新增成本。

## 原理与分析

首 token 路径可写为 `T_aggregate=Q_A+P_A+first_decode_A` 和 `T_split=Q_P+P_P+Q_X+transfer+Q_D+first_decode_D`。Routing、缓存预留、组批使这些项相关；消除干扰超过新增排队和交接才值得分离。

32K prompt 若在选中 D 命中 31K，只剩 1K 本地 prefill，远端 P 可能为很少的计算节省付出大量状态传输。冷 32K 则可能长期占住混合引擎。命中属于真实目标，不能用抽象全局 hit rate 代替。

角色数量是较慢决策，可从 SLO 容量 `n_P=ceil(λ/μ_P)`、`n_D=ceil(λ/μ_D)` 开始；长度、命中和批占用会改变两个容量。

## 设计

分别处理逐请求条件路由、副本扩缩容和活跃 worker 角色转换；前两项不证明第三项。路由根据有效输入长度和负载决定，支持时加入 decode-busy override。交接持有状态直到传输完成，并选择兼容 D。

引用 Dynamo 条件路径支持 vLLM、TensorRT-LLM，不支持 SGLang；planner 分别估角色容量，并处理 ready／pending。llm-d 前缀决策器处理 `if-available`，可用 HTTP 412 触发远端 prefill，Production Stack 也提供 P/D 编排。Foretoken workflow 在 P 完成后重新选 D，并保留跨阶段清理。

## 实现

条件路径的核心过程为：

```text
观察兼容D候选、缓存状态与负载
有效输入 = 输入token - 目标可复用前缀
估计本地prefill干扰和远程交接成本
若有效工作值得分离或触发decode-busy条件:
    选择已就绪P，执行远端prefill
    保留源KV直到交接结束
    选择兼容D，传输所需KV
    目标状态就绪后开始decode
否则在选中的聚合路径本地prefill/decode
完成或取消确认后回收所拥有状态
```

这是决策结构，不是通用长度阈值。权重／KV dtype、TP、connector 限制交接。BF16 dense/GQA 与 MoE、hybrid state 条件不同；FP8 权重不保证 FP8 KV，host-staged TCP 还增加复制。

## 影响与效果

分离消除长 prefill 对已有 decode 的干扰，条件路由避免为高度复用 prompt 传输，但不消除 D 饱和或链路排队。短输入、低并发、大量本地命中、慢中转可能更适合 chunked-prefill 聚合引擎。

GPU 成本包括全部 P/D worker、重复权重、启动和退役；D-only trace 变快不证明完整服务更便宜。32K／31K 是有效工作示例，不是实测延迟。分别估角色容量能随负载变化，不依赖固定 P:D。缓存／负载观测滞后仍可能使预测优势在执行前消失。

## 参考

- [条件分离文档](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/docs/fern/pages/developer-guide/advanced-customizations/conditional-disaggregation.md)
- [吞吐规划代码](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/core/throughput_scaling.py)
- [前缀决策器](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/scheduling/profilehandler/disagg/prefix_based_pd_decider.go)
- [路由实现](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py)
- [阶段执行](../../../../data-plane/frontend/src/server/src/runtime/workflow.rs)
- [扩缩容 owner](../../../../control-plane/internal/autoscaling/README_zh.md)
