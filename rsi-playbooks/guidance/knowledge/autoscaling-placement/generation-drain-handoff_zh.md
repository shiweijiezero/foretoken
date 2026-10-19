<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 保持在途生成连续：排空与状态交接

[English](generation-drain-handoff.md) | 简体中文

## 现象与动机

移除 decode worker 可能打断数分钟生成并丢失暖 KV。慢慢减少副本，不会自动停止新派发或保留已接受工作。退役需要分别完成关闭准入、撤回路由、请求结束和状态释放；持久前缀不是可续跑请求的 checkpoint。

## 原理与分析

先关闭准入，再撤回路由，能阻止滞后 router 继续加入工作。退役随后等待观察者安装撤回状态和已有请求完成。期限限制容量滞留，但过期表示未完成排空，不是零损失成功。

自然排空保留执行，也继续占用 source GPU 直到剩余生成结束。在线迁移只有在兼容 destination 有余量、暂停可接受时才缩短占用。4 GiB 经有效 25 GB/s 链路复制，payload 下界约 172 ms，不含同步和复制期间新增状态；这是算术示例，不是迁移测量。

兼容性涉及 checkpoint、KV dtype、TP/DP 布局和 runtime 状态。GQA、MLA、循环缓存不同，权重兼容不等于 cache layout 兼容；迁移还会争用剩余 worker 的推理链路。

## 设计

以关闭准入和确认路由撤回作为退役边界。已接受请求保持 source ownership，直到完成或完成受支持、经确认的交接。共享前缀遵循独立存储生命周期，不能替代活跃请求位置、采样状态和清理职责。

Controller 拥有 worker 退役，frontend 拥有路由观测与请求清理，引擎拥有续跑状态。Foretoken Group drain 关闭 model-server 准入，检查每个存活 frontend 的 snapshot generation，观察运行请求，在完成或记录 timeout 后释放 finalizer。P/D、E/P/D 工作流保留跨阶段清理；这是退役，不是在线迁移。

## 实现

自然排空的步骤为：

1. 关闭 source 准入，发布路由撤回。
2. 等待存活 frontend snapshot 确认撤回 generation。
3. 观察已接受请求，直到归零或排空期限到达。
4. 分别记录完成与超时，再释放退役 finalizer 和资源。

引擎支持迁移时，先预留兼容 destination，复制已有 KV 并追赶新增写入。在一致执行边界暂停，传递 token 位置、sampling RNG、推测／混合状态与请求生命周期；交接 ownership，取得 destination 确认后释放 source。重发 prompt 是 replay，不是透明续跑。

Dynamo shutdown 工具注销 endpoint、调用有期限的可选 callback，排空语义由 callback 决定。引用 AIBrix helper 注解 Pod 并超时删除，不验证引擎归零；Production Stack 刷新 endpoint 本身不迁移活跃状态。

## 影响与效果

经确认的排空避免新请求与退役竞态，在期限内完成时保留已接受生成，代价是继续占用 GPU；本地缓存丢失还会影响后续请求。迁移用搬运字节、destination 内存和暂停开销换取更早释放。TRE 的 A100 40GB、7B/8B/14B hot-switch 原型报告秒级 sleep/wake，但 drain 失败后通过重发恢复而非迁移 KV；切换结果不证明生成无中断交接。

## 参考

- [Foretoken Group drain owner](../../../../control-plane/controllers/model/drain.go)
- [Foretoken 请求工作流](../../../../data-plane/frontend/src/server/src/runtime/workflow.rs)
- [Dynamo graceful-shutdown 工具](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/common/utils/graceful_shutdown.py)
- [AIBrix drain helper](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/pkg/controller/drain/drain.go)
- [Production Stack router 代码](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py)
- [TRE 论文](https://arxiv.org/html/2609.29160v1)
- [KV 缓存](../kv-cache/)
