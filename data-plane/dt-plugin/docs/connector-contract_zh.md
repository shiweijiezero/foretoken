<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# DT Connector：提议分布与所有权

[English](connector-contract.md) | 简体中文

Connector 连接独立的 Draft、Target Worker。HTTP 传递请求控制、候选 token 和不可变
张量描述符；Mooncake 将 Draft 的提议分布读入 Target GPU。
[角色协议](role-protocol.md)说明控制接口，[引擎契约](mrv2-integration.md)说明 MRV2 接入。

## 跨角色传递什么

| 方向 | 内容 | 生产者与消费者 |
| --- | --- | --- |
| 前端 → Draft | 确认前缀、版本、采样参数、候选预算 | 前端轮次编排 → Draft 服务 |
| Draft → 前端 → Target | 候选 ID、版本、artifact ID | Draft 服务 → Target 服务 |
| Target → Draft → Target | claim artifact ID，取得 `PayloadRef` | Target 服务 → Draft Worker → Target 服务 |
| Draft GPU → Target GPU | 连续 float32 `log(q)`，形状为 `[候选数, 词表大小]` | Draft 采样器/Worker → Target Worker/原生拒绝采样器 |
| Target → Draft | 携带 publication ID 的读取完成确认 | Target 服务 → Draft 释放接口 |
| Target → 前端 → Draft | 精确确认的 token 增量、下一版本或终止结果 | Target 输出处理 → Draft 确认前缀 |

每一行分布对应一个候选位置的真实随机抽样，已经包含 Draft 温度和所支持截断规则的
影响。它既不是单个候选 token 的概率，也不是未处理的模型 logits。
贪心提议在选中 token 上使用 log 概率 0，其余位置为负无穷。
双方必须具有相同的词表大小和 token ID 含义。

`PayloadRef` 包含 `publication_id`、`segment`、`address`、`nbytes`、`dtype`、`shape`。
这些字段描述存储位置，不表达候选是否被接受。Target 自行分配本地 GPU 缓冲区并校验
布局；前端不会读取张量内容，也不会把概率序列化成 JSON。

## Worker 与引擎如何衔接

1. Draft 的 MRV2 采样器提供处理后的 logits；Worker 扩展在 GPU 上保存完整 `log(q)`，
   等待生产者 CUDA event 后发布描述符。
2. Target 先向 Draft claim artifact，取得描述符，再通过 Worker RPC 发起并轮询 Mooncake 读取。
   传输工作在模型执行之外进行；
   数据尚未就绪时，对应请求不会进入验证调度。
3. 读取完成后，Target 确认源 publication，并把目标 artifact 绑定到引擎请求 ID 和
   generation；随后才调用 `submit_external_draft_batch`。
4. Scheduler 组织本地 batch；Runner 按当前请求槽位和候选位置映射分布，不能按传输
   到达顺序推断 batch 行号。
5. 适配器把 `log(q) * Target 温度` 交给原生拒绝采样器，抵消其内部的温度除法。
   贪心行使用单位缩放，保持已规范化提议分布的含义。
6. Target 保留 artifact，直到下一次确认输出或请求 abort。DMA 后源端可释放，与
   模型消费后目标端可释放，是两个不同的时点。

验证、采样、停止由 Target 负责。候选提交成功只表示进入验证，最终 token 必须来自
Target 输出。Draft 随机数与 Target 独立；用户 seed 控制 Target，不保证不同候选轮次
安排会产生相同输出。

## 内存、取消与排空

只有 claim 成功后，Target 才能取得源端描述符。Draft Worker 串行处理 claim 和取消：
如果取消先执行，未领取的 artifact 会被撤销，本地 GPU 工作完成后即可回收 buffer；
后来的 claim 会被拒绝。如果 claim 先执行，源端保持内存注册且不可覆盖，直到收到
对应 publication 的 ACK。
HTTP 请求取消不会取消 DMA；因此 Target 的 claim/读取/ACK 任务会完成必要清理，即使原控制
调用已取消。旧 ticket 阻止后续推理消费，不取消传输清理。
如果 Target 在发起任何读取之前放弃 artifact，也会发送 ACK。
已领取的源端 buffer 需等待 ACK 和最后一次本地 GPU 使用完成，随后交还给按
容量和 dtype 管理的 Worker 缓冲池。特征前缀按几何增长的行容量复用，只发布已填充的行，
接收端可以读入行容量更大的已注册缓冲区；其他载荷按精确形状复用，避免每种前缀长度都
保留一个完整特征缓冲区。注册跨轮保留；每种容量的缓存数量达到该容量的
并发高水位后保持到角色关闭。空闲缓冲区不计入 retained artifacts。
状态不确定的传输和已领取但未收到 ACK 的发布不会提前回收；对端失败时，可能需要终止所属进程。

`/status.retained_artifacts` 单独统计传输持有的产物，`active_sessions` 统计会话。
控制器遥测 `running_requests` 仍是会话数。model-server 在已有关闭时限内等待两者
归零，不提供透明的会话迁移或失败重放。

## 支持范围与未实现的方法

RDMA 角色公布 `token_ids_log_probs`。无 RDMA 的角色公布 `greedy_token_ids`，只接受
温度 0。Router 先筛选同一 scope 内 format 一致的 DT，随机采样还会排除仅传 token 的角色；
执行前会再次检查两端状态，不会把需要概率张量的请求自动降级成仅传 token。
当前每个角色使用单 GPU Worker、eager 执行、本地同步调度。

显式 Target migration 使用 uint8 KV checkpoint，复用 artifact 的 claim/read/ACK
生命周期。两个 Target 都需启用 migration，并使用兼容的模型和 layer layout。
source 复制 native KV pages；destination 用自己的 scheduler 分配 pages，再加载收到的
副本。详见 [checkpoint 协议](role-protocol.md#target-kv-checkpoint-and-resume)。

DFlash、Qwen3 DSpark、EAGLE/EAGLE3 和单模块 MTP 已接入 Target 特征导出和原生 Draft 消费者，
BF16/FP16/FP32 特征复用相同的
artifact 生命周期。前端在准入前检查两端 context profile 一致；其他变体尚不在
[神经 Draft 支持范围](../README_zh.md#在-nvidia-上运行神经-draft)内。候选树需要分支结构、
tree attention、已选路径清理；串联验证需要阶段依赖及中间验证语义。KV 共享和卸载
还需要各自的 placement 和所有权路径；checkpoint 操作或增加描述符都不能直接实现它们。

两机随机采样正确性、取消及资源释放必须通过真实模型链路验证。
性能和完整 Kubernetes 生命周期验收也不能由独立 tensor 诊断代替。
