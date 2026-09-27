<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# DT Connector：方法载荷与引擎边界

[English](connector-contract.md) | 简体中文

目的：确定分离后哪些信息跨角色传递、谁生产和消费，以及哪些引擎接口必须补齐。
本文件是供评审的后续接口设计；当前可执行协议仍是 [greedy role protocol](role-protocol.md)。
**现有 HTTP greedy 闭环与独立 RDMA 诊断，不等于通用 DT Connector 已交付。**

## 交付边界

Frontend 选择角色、安排阶段依赖、持有请求生命周期；Connector 传递控制信息和数据；
各引擎负责模型执行、batch 和 KV。Target 是最终 token 提交者。连接器不负责判定
接受概率，也不把 Draft 的阶段输出当作最终结果。

控制消息经前端协调；大张量在生产和消费角色之间通过 Mooncake 直接传输。
前端只转发描述符，不能把 logits 或 hidden states 读回后再通过 JSON 转发。
Mooncake 解决字节传输，不定义张量的算法语义。

## 各方法的数据要求

| 方法 | Target → Draft | Draft → 验证者 | 当前缺口 |
| --- | --- | --- | --- |
| 独立模型，greedy 线性候选 | 已确认上下文、轮次、预算、停止结果 | 候选 token | 已有 HTTP 路径；尚无推理中的 RDMA 载荷 |
| 独立模型，随机投机采样 | 上述上下文及采样契约 | 候选 token 与实际提议分布 q，按候选位置和词表对齐 | Draft 的 q 导出、Target 概率消费、Worker 张量接入 |
| 特征驱动 Draft | 方法指定层的 hidden states/特征、对应 token/位置 | 候选及该方法的验证载荷 | Target 特征导出、Draft 特征输入；需要匹配的模型对 |
| 多分支/tree | 分支所基于的确认上下文 | token、父节点、位置、方法要求的分支概率 | 聚合、tree attention、树验证与已选路径回收 |
| 串联 Draft/中间验证者 | 上一阶段产物及其上下文依赖 | 新候选产物 | 阶段编排及中间验证语义；仅最终 Target 提交输出 |

随机采样的 q 必须对应真正生成候选的分布，包含温度、截断、惩罚等变换的影响。
仅候选 token 的标量概率一般不足以完成拒绝后的修正采样。传 raw logits 还是规范化
概率必须与验证器约定，不能用同一个未注明语义的 `logits` 字段代替。
确定性提议也可以对应点质量分布；载荷取决于提议方法，而非只看 Target 的温度。

独立模型通常各有 KV，不能根据 token 一致就直接互拷。KV 共享、offload、迁移需要
另行约定模型/层、缓存布局、位置及所有权，不属于所有 DT 方法的必传信息。
多模态也不能统一视为“只影响 prefill”：Draft 如何获得已编码上下文仍取决于方法。

## 拟定的控制与数据契约

保留 Open、Propose、Verify、Commit、Cancel 的职责，采用方法标识的显式载荷类型，
不增加任意 `dict` 或一组可随意组合的 nullable tensor 字段。以下是设计，不是已开放 API：

| 对象 | 必须表达的信息 | 权威所有者 |
| --- | --- | --- |
| Session | 模型对、tokenizer/token 语义、方法及版本、采样契约 | 前端绑定；双方引擎校验能力 |
| Task | 请求/会话、阶段、轮次、base context version、产物身份、预算 | 前端安排依赖；Target 授予有效验证轮次 |
| Candidate artifact | 方法类型、线性或树结构、该方法要求的数据引用 | 生产阶段；不能自行推进已确认上下文 |
| Context artifact | token/位置范围、特征层和语义、数据引用 | Target 或指定上游阶段 |
| Tensor reference | publication ID、segment、地址、字节数、dtype、shape | 生产 Worker/传输 owner |
| Commit | 精确 token delta、终止结果；树方法还需选中路径 | 最终 Target |
| Transfer completion | 精确 publication ID 对应的读取完成确认 | 接收端传输 owner；不同于候选接受结果 |

当前 `PayloadRef` 已实现 dtype/shape/字节数一致性检查，仅接受非空连续张量；
接收缓冲区布局必须完全匹配。方法语义、位置和词表映射属于 artifact，不能由 shape 推断。
本地设备、stream、接收地址由消费者选择；生产者的设备编号不作为远端分配命令。

新方法只有在“生产者导出、传输、消费者导入、验证算法、清理”全部支持时才可公布能力。
当前只有 `greedy_token_ids`；不提前宣称随机分布、特征或 tree 可用。协议升级时，双方和
前端必须一致拒绝未知版本/方法，不能降级为 token-only 后继续运行。

## 引擎接入与 batch

| 阶段 | 必须复用/补齐的边界 |
| --- | --- |
| 导出 | Worker/Runner 提供真实 q 或特征及布局；记录完成事件后才发布引用，不能从公开 top-logprobs 反推完整 q |
| 传输 | Connector 分配/注册本地接收存储、完成 RDMA、确认源端读取完成；与计算线程分离 |
| 就绪 | Worker 完成布局/设备物化，通过 EngineCore 事件通知 Scheduler；收到描述符不等于可执行 |
| 批处理 | Scheduler 组验证 batch；Runner 在最新请求槽位上建立位置映射和张量视图，不使用传输到达顺序当作 batch 行号 |
| 验证 | 复用 MRV2 对应采样器；当前外部 token 接口还需增加概率/特征的设备输入生命周期 |
| 提交 | 沿用输出停止处理后再发布确认；前端据此安排下游任务 |

请求等待远端数据时不阻塞其他请求。异步远端等待与 vLLM 的 async scheduling 是两个
维度，不能把打开后者当作已实现前者。多 GPU 还需要明确 shard 和执行 rank，目前单 Worker
模式不提供该能力。批量传输可以优化多个 artifact，但不能合并它们的请求/轮次身份。

## 内存与故障

1. 生产者完成本地写入并发布；发布期间保持注册且不可覆写。
2. 接收者等待接收缓冲区的旧消费者完成，再提交读取；读取完成后才能使用数据并 ACK。
3. ACK 仅允许源缓冲区回收，不表示模型接受候选，也不表示接收端计算结束。
4. 接收端最后一个计算消费者完成后，才可复用或注销接收缓冲区。
5. 取消/旧轮次使 artifact 不再参与推理，但不会取消正在发生的 DMA；仍须完成传输清理。
6. 连接中断或传输状态不确定时，不因超时释放已发布内存。当前隔离保留策略可能占用资源直至
   进程退出；可回收的 peer-failure 协议仍需设计，不能宣称支持无损故障恢复。

排空先停止新会话，再完成/取消已有任务及传输。扩缩容作用于独立角色池；迁移到新实例
意味着新会话和状态恢复，不能沿用旧地址、publication 或 EngineCore ticket。

## 下一步验收顺序

先将真实模型产物经 artifact 引用、Mooncake、消费适配器接成一条两机路径，验证
输出、在途取消、排空和注册释放；只传 token 的成功不能代替概率/特征方法验收。
随后接入独立随机 Draft 的真实 q 导出和 Target 验证，比较 Target 分布并测量通信开销。
特征型方法、tree 与串联验证分别需要相应模型及算法，不能靠增加 Connector 字段宣布完成。

参考：[vLLM 投机解码](https://docs.vllm.ai/en/latest/features/speculative_decoding/)、
[Mooncake Transfer Engine](https://kvcache-ai.github.io/Mooncake/design/transfer-engine/index.html)。
代码对照基线：vLLM `1be3628` 的 MRV2 `RejectionSampler` 接受 `draft_logits`，
而当前外部候选扩展只提交 token，未接通该张量输入。
