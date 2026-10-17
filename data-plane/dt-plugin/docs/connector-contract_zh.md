<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# DT Connector：提议分布与所有权

[English](connector-contract.md) | 简体中文

Connector 连接独立的 Draft、Target Worker。HTTP 传递请求控制、候选 token 和不可变
张量描述符；Mooncake 通过 RDMA 读取设备 tensor，ZMQ TCP/IPC 则经过主机内存中转；MACA GPU IPC 复用 Mooncake，在同机且双向
授予 peer 访问的 GPU 间复制。
[角色协议](role-protocol.md)说明控制接口，[引擎契约](mrv2-integration.md)说明 MRV2 接入。

## 跨角色传递什么

| 方向 | 内容 | 生产者与消费者 |
| --- | --- | --- |
| Target → Draft | 确认前缀、版本、采样参数、候选预算 | TargetCoordinator → Draft 服务 |
| Draft → Target | 候选 ID、版本、artifact ID | Draft 服务 → Target 服务 |
| Target → Draft → Target | claim artifact ID，取得 `PayloadRef` | Target 服务 → Draft Worker → Target 服务 |
| Draft GPU → Target GPU | 连续 float32 `log(q)`，形状为 `[候选数, 词表大小]` | Draft 采样器/Worker → Target Worker/原生拒绝采样器 |
| Target GPU → Draft GPU | 已确认计算前缀的特征 `[tokens, feature_layers, hidden_size]` | Target 特征收集器 → 原生神经 Draft |
| Target → Draft | 携带 publication ID 的读取完成确认 | Target 服务 → Draft 释放接口 |
| Target → Draft | 精确确认的 token 增量、下一版本或终止结果 | Target 输出处理 → Draft 确认前缀 |

每一行分布对应一个候选位置的真实随机抽样，已经包含 Draft 温度和所支持截断规则的
影响。它既不是单个候选 token 的概率，也不是未处理的模型 logits。
贪心提议在选中 token 上使用 log 概率 0，其余位置为负无穷。
双方必须具有相同的词表大小和 token ID 含义。

`PayloadRef` 包含 `publication_id`、`transport`、`segment`、`address`、`nbytes`、`dtype`、`shape`。
RDMA 和 GPU IPC 使用 Mooncake segment 和正整数设备地址；ZMQ 使用 TCP 或 IPC endpoint 和 null 地址，
按不可变 publication ID 读取。
这些字段描述存储位置，不表达候选是否被接受。Target 自行分配本地 GPU 缓冲区并校验
布局；前端不会读取张量内容，也不会把概率序列化成 JSON。

## Peer 控制与传输归属

每个 role 的 `RoleCommunication` 统一持有 HTTP client 和连接池，供控制消息、
feature export、tensor claim/read/ACK 和 RDMA 验证复用。每个请求的
`DraftPeerSession` 只持有自己的远端 session、NDJSON stream 和单一 close task；
关闭该 session 不会关闭共享 client，也不会中断其他请求。

`TargetCoordinator` 决定何时 propose、commit、revoke 和 verify，并管理确认进度，
不再构造 URL 或管理 HTTP 资源。`RoleService` 继续负责模型上下文、tensor 布局和
engine ticket 校验，之后才 staging 或 admission。通信 owner 独立持有已启动的
read/ACK 操作；成功完成后，调用方接管目标数据的 staging 生命周期。物理 buffer
和完成 fence 仍由 Worker transport 持有。

关闭 session 会等待原生输入预处理和提交完成，再 abort 并等待 GPU 工作结束；
checkpoint 接收 buffer 也保留到这一边界。输入拒绝只作为请求错误返回，关闭仍执行
abort 和 GPU fencing；真实的清理失败继续向上传递。神经 Draft 配对使用 preparation 冻结的
checkpoint revision，服务发现仍使用请求的模型身份；独立启动时使用原生配置解析的
commit，本地路径必须对应同一份不可变权重。

role 退出时，先保持 HTTP 连接池可用，等待请求关闭和共享通信操作完成，以便
发送关闭消息及 ACK；随后执行既有 Worker drain。role lifespan 最后关闭连接池，
再停止 engine。失败或完成不确定的传输继续沿用隔离与进程退出边界。连接池不另设
请求容量限制，长生命周期 session stream 不会耗尽一套额外的连接额度；现有
admission 继续负责请求容量。

## Worker 与引擎如何衔接

1. Draft 的 MRV2 采样器提供处理后的 logits；Worker 扩展在 GPU 上保存完整 `log(q)`，
   等待生产者 CUDA event 后发布描述符。
2. Target 先向 Draft claim artifact，取得描述符，再通过 Worker RPC 发起并轮询所选传输方式的读取。
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

tensor 角色公布 `token_ids_log_probs` 及已初始化的 `tensor_transports`。
Router 要求有兼容的能力交集；GPU IPC 还要求 host identity 一致且双方
`gpu_ipc.peer_devices` 相互可达。Target 优先选择合格的 GPU IPC，然后是 RDMA、ZMQ，并将
选择传给 Draft session 和 Target 特征导出。每个 artifact 从发布到读取、释放都保留
原 transport。auto 提供 ZMQ TCP、明确分配的 RDMA connector，以及设备权限和原生 peer 矩阵
允许的 MACA GPU IPC；IPC 保留为共享
同机目录的显式选项。auto 在 Mooncake 缺失或 RDMA 初始化返回失败时记录原因，
在模型 tensor 传输之前从能力中移除 RDMA；本地 GPU 注册失败也会移除该路径。
强制 RDMA 模式会启动失败。编程错误和在途失败
不会触发降级。绑定日志记录
request、两端地址及所选路径。TCP 描述符包含 Worker 实际绑定的端口。
无 tensor 传输的角色公布 `greedy_token_ids`，只接受
温度 0。Router 根据 Target 引用的 Draft pools、format、传输方式和 context profile 检查兼容性，
Draft 和 Target 均须属于调用者允许的 Pool 范围；
随机采样还会排除仅传 token 的角色。调度策略为选定的 Target 分配 Draft，Target 在执行前检查已分配 Draft 的实时能力，不会把需要概率张量的请求自动降级成仅传 token。
当前每个角色使用单 GPU Worker、eager 执行、本地同步调度。

显式 Target migration 使用 uint8 KV checkpoint，复用 artifact 的 claim/read/ACK
生命周期。两个 Target 都需启用 migration，并使用兼容的模型和 layer layout。
source 复制 native KV pages；destination 用自己的 scheduler 分配 pages，再加载收到的
副本。详见 [checkpoint 协议](role-protocol.md#target-kv-checkpoint-and-resume)。

DFlash、Qwen3 DSpark、EAGLE/EAGLE3 和单模块 MTP 已接入 Target 特征导出和原生 Draft 消费者，
BF16/FP16/FP32 特征复用相同的
artifact 生命周期。前端在准入前检查两端 context profile 一致；其他变体尚不在
[神经 Draft 支持范围](../README_zh.md#运行神经-draft)内。候选树需要分支结构、
tree attention、已选路径清理；串联验证需要阶段依赖及中间验证语义。KV 共享和卸载
还需要各自的 placement 和所有权路径；checkpoint 操作或增加描述符都不能直接实现它们。

两机随机采样正确性、取消及资源释放必须通过真实模型链路验证。
性能和完整 Kubernetes 生命周期验收也不能由独立 tensor 诊断代替。

## 同机 GPU IPC

MACA runtime 会缓存设备可见性，包括 vLLM-MetaX 传递的 `MACA_VISIBLE_DEVICES`，
因此 adapter 在短暂的独立元数据进程中
查询 peer 能力。render-device 权限检查将查询限制在容器获授的设备；计算 Worker
保留原绑定。能力包含 host boot identity `host_id`、不透明的 `device_id` 和
`peer_devices`；发现和实时绑定检查同一双向关系。它不会修改 Kubernetes 的 GPU
分配或设备权限，因此当前每 Pod 一卡的隔离方式通常仍选择 RDMA/ZMQ。

`MacaIpcTransport` 复用 Mooncake 的 IPC handles、allocation-base 地址重定位和
拷贝完成反馈。同一底层 allocation 只注册一次，最后一个保留的 Torch 子分配关闭
后才注销。该引擎初始化后恢复镜像的 host-transport 设置。producer event、
claim/read/ACK、按 backend 区分的缓冲池和 consumer fence 沿用现有 owner。
已发布的 descriptor 不会切换 backend。round 的 `tensor_transport` 记录绑定选择，
transfer 的 `transport` 记录实际完成读取的 backend；它们不代表物理链路计数或 GPU
kernel 时间。

Mooncake adapter (RDMA and MACA) 在提交前，使用 Python engine 的 native pointer 调用 Mooncake 公共
C API `openSegmentNoCache`，刷新该 peer 的注册信息，覆盖首次打开 segment 后
新发布的 allocation；不会重试或切换已提交的传输。model-server 的 CUDA 构建和
MetaX runtime 构建都会在链接固定版本的 Mooncake engine 时保留该 C API。
同一 peer 的刷新和提交串行交接，避免并发刷新在 read 提交前覆盖注册描述。
拷贝完成轮询仍然并发，其他 peer 使用独立提交锁。

当前接入 MACA，尚未启用 NVIDIA CUDA IPC/NVLink。选择仅用于概率和 features；
手工 KV checkpoint 仍要求严格的 NVIDIA RDMA 启动模式和既有限制。必须在实际
容器配置下验证 peer 能力及内存导入、拷贝；仅共享 IPC namespace 不代表设备权限可用。

## RDMA 绑定验证

本地初始化和 GPU 注册不能证明对端可达。通信层在 Target prefill 前建立选中 GPU 对的
双向验证。`/transport/rdma/probe` 与 `/transport/rdma/probe/read` 复用 Worker 的
artifact claim/read/ACK、GPU 内容校验和 buffer 回收。并发请求共享一次正在执行的
验证，后续请求复用成功结果。取消一个等待请求只关闭该请求的 session，不取消共享
验证，也不释放它持有的资源。
验证失败时，连接撤销未领取的 probe，并等待其本地完成 fence；已领取的 probe
保留原归属，直到 ACK 或所属进程退出。

`/status.rdma` 包含实际 Worker Mooncake engine 的实例身份和 segment。身份在引擎
初始化时生成，对应该 GPU、NIC 配置和 backend；进程重启或引擎重建后即使 HTTP
地址相同，身份也会改变。两端在发布和读取前校验预期身份，Draft 的 opened 事件
再次返回身份，避免 status 与 session admission 之间重启造成误复用。通信 owner
通过单一 close task 退役被替换或失败的 peer：等待已启动操作，撤销未领取的 probe，
已领取的 publication 继续保留 ACK 归属，不取消在途操作。不使用 TTL、设备扫描或每请求测速。

固定 Mooncake Python adapter 提供 segment handle 和完成轮询，没有 peer 重连通知。
`openSegmentNoCache` 刷新注册描述不代表重建 transport。RDMA read 失败会使该 peer
的验证失效；既有 `/status` 绑定检查同时传递失败 peer 的实例身份，使 Draft 接收端
发现的故障也能使 Target 的缓存验证失效。后续请求失败，直到 transport 重新建立。
本次不自动重试、切换路径或
透明恢复。role 退出先等待共享操作，再 drain Worker transport。完成不确定的 read
继续保留注册，直到完成证明或所属进程退出；断连不能作为 buffer 可复用的依据。

发现中的 `rdma` 表示本地 HCA 和实际 Worker GPU 注册通过。
`/status.rdma_connections` 记录 peer 数、发起的双向验证次数和未完成通信操作数；
`DT RDMA peer verified` 日志记录两方向实际结果。这些检查不代表 GPUDirect 带宽或
性能已验收。

持续运行诊断可读取 `/status.transport` 中的 registered buffer 数量及字节数、
idle buffers、pending reads 和未 ACK publication 数量。字节数是保留 tensor 的容量，
不等于 allocator 的唯一物理分配量。可复用的空闲注册与
存活 artifact 分开计数；资源清零不能替代 API/engine/worker RSS 的持续观测。
