<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Draft/Target 服务

[English](README.md) | 简体中文

将用户指定的 Draft、Target 模型分别运行在不同 GPU 或主机上。前端选择一对实例，
协调候选生成和验证，只向用户输出 Target 确认的结果。Mooncake 在 GPU Worker 之间
传输 Draft 的完整提议分布；HTTP 传递候选 token 和张量描述符。两端各自管理调度和 KV。

通过现有 Controller、Router 部署请使用 [Kubernetes 示例](../../examples/draft-target/README_zh.md)。
下面直接启动两个角色服务，再通过 [Rust 前端示例](docs/frontend-workflow.md)发送请求。

## 启动两个角色

两台主机均需安装独立的 [vLLM 引擎扩展](https://github.com/shiweijiezero/vllm/pull/1)，
并准备 Python 3.10+、兼容的 PyTorch/Mooncake wheel 和可用的 GPU/RDMA 访问。
环境还需包含 FastAPI、Uvicorn、AnyIO、Pydantic 2 和 HTTPX。
安装本包不会安装或修改 vLLM，也不会注册 `vllm serve` 模式。

使用已加载的 `nvidia-peermem` 内核模块时，两端进程设置 `WITH_NVIDIA_PEERMEM=1`；
否则 Mooncake 默认的 DMA-BUF 路径需要相应驱动支持。容器还需具备 GPU/RDMA 设备访问、
RDMA 用户态库和足够的锁定内存额度。

将 `DRAFT_MODEL`、`TARGET_MODEL` 设为模型名称或本地路径。两模型必须具有相同的词表
大小和 token ID 含义。`TOKENIZER` 指向双方共同使用的 tokenizer，通常来自 Target。
`DRAFT_IP`、`TARGET_IP` 分别为两台主机的本地可路由地址，不含端口。

两端都在仓库根目录安装：

```bash
python -m pip install --no-deps ./data-plane/dt-plugin
```

Draft 主机运行：

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role draft \
  --model "$DRAFT_MODEL" --tokenizer "$TOKENIZER" \
  --host 0.0.0.0 --rdma-host "$DRAFT_IP"
```

Target 主机运行：

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role target \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --host 0.0.0.0 --rdma-host "$TARGET_IP"
curl http://127.0.0.1:19100/status
```

`/status` 应返回正确的模型和角色、`accepting: true`，以及
`candidate_format: "token_ids_log_probs"`。前端选中的两个角色必须使用相同格式。
使用两端 HTTP 地址启动前端；角色服务仅提供内部 API，没有公共 OpenAI 接口或鉴权。

每个 RDMA 角色使用一个 GPU Worker、eager 执行和本地同步调度。等待远端候选只暂停
对应请求，其他请求仍可执行。命令接受 vLLM 原有的模型、显存及 batch 参数。
`--draft-token-budget` 设置每轮候选上限，默认 3；`--port` 设置 HTTP 端口，默认 19100；
`--rdma-nic` 可指定 HCA，不设置时由 Mooncake 从可见设备中选择。
HTTP 端口和 Mooncake 动态分配的握手端口需要互通。

角色启动器默认在两端设置 vLLM 原生 `VLLM_BATCH_INVARIANT=1`，以减少 batch 形状引起的
数值差异。该模式文档支持的硬件范围是计算能力 8.0 及以上的 NVIDIA GPU，模型和
attention 后端也必须支持它。启动前显式设置 `VLLM_BATCH_INVARIANT=0` 可关闭；关闭后
贪心输出可能随 batch 组合变化。与独立 Target 对比时应使用相同设置。
当前未测量这一设置的性能成本，也不承诺所有配置都能逐 token 完全一致。

支持 `temperature`、`top_p`、`top_k` 和 Target 的 `seed`。
Draft 使用独立的随机数；固定 Target seed 不保证不同候选轮次或 batch 组合产生相同序列。

不使用 RDMA 时，两端都省略 `--rdma-host`。此时仅支持 `temperature: 0`，
能力标识为 `greedy_token_ids`，候选通过 HTTP 传递；随机采样请求会在准入前被拒绝。

## 停止服务与支持范围

`POST /drain` 关闭新请求准入，允许已有会话完成。关闭前端输出流可取消请求。
正常停服前，应等待 `/status` 中的 `active_sessions` 和 `retained_artifacts` 都归零。
GPU 缓冲区按精确形状复用，注册保留到角色关闭，因此会话结束后显存可能仍保持
各形状的并发高水位。未收到读取确认的发布或完成状态不确定的 DMA 可能保留内存直到进程终止；
当前不提供透明的对端故障恢复。

当前每个请求使用一个 Draft 和一个 Target，处理文本输入及线性候选。
尚未实现 hidden-state 方法、候选树、串联验证、多模态、P/D 组合、KV 传输或卸载。
传输诊断或生成成功不代表存在加速收益，也不代表 Kubernetes、自动扩缩容已完成验收。

## 运行两机校验

使用两台具有 Python 3.10 或以上版本、可用 RDMA 的 Linux 服务器，确保控制端口和 Mooncake RPC 端口可达，
并使用引擎环境中的 PyTorch 和 Mooncake wheel。传输适配针对仓库固定的 Mooncake
提交 `719735896c86b56fabec6cf3e825fb2ea640597a`。使用平台提供、与加速卡匹配的
wheel；安装本包不会替换该 wheel，也不会安装 vLLM。GPU 内存注册需要硬件与驱动支持。
A100 跨机验证使用 `mooncake-transfer-engine-cuda13==0.3.13.post1` 和 PyTorch
`2.11.0+cu130`。CUDA 12 的 Mooncake wheel 不能直接当作 CUDA 13 wheel 使用。

两台机器都在仓库根目录执行：

```bash
python -m pip install --no-deps ./data-plane/dt-plugin
```

将 `PRODUCER_IP` 设置为生产端可路由 IP，`CONSUMER_IP` 设置为消费端可路由 IP，
均不带端口。在生产端运行：

```bash
foretoken-dt-transfer serve --host "$PRODUCER_IP" --device cuda:0
```

看到 `listening` 后，在消费端运行：

```bash
foretoken-dt-transfer read --host "$CONSUMER_IP" --peer "$PRODUCER_IP" --device cuda:0
```

两端须使用诊断协议第 2 版，张量描述符包含 dtype、shape 和字节数。
生产端发布 int64 tensor；消费端拉取到本地设备，确认读取完成，再逐元素校验。
成功后消费端输出 `verified: true`、字节数、设备和传输耗时，两个进程退出。
命令接待一个消费者，使用 TCP 19090 交换描述和确认消息，Mooncake 采用 P2P
握手，无需独立元数据服务或 Mooncake Store；tensor 内容不通过控制 socket。

通过 `--nic` 选择 RDMA 设备，通过两端的 `--port` 修改控制端口；生产端的
`--elements` 改变数据大小。生产端 `--iterations 4` 在同一组源/目标注册缓冲区上
连续发布四轮，每轮改变内容以检查旧数据误用；输出 `bytes` 是每轮字节数，
`transfer_seconds` 是所有轮次读取耗时之和。`--device cpu` 可诊断主机内存传输，但不能验证 GPU
直达。控制消息假设来自部署网络中的可信对端。

## 成功意味着什么

传输明确请求 RDMA，没有应用层 TCP 降级。仍需结合 Mooncake 日志和设备/网络计数器
确认实际路径，不能仅根据 `requested_transport` 认定 GPUDirect RDMA 或无中转。
耗时包含首次建连和轮询开销，不代表吞吐基准或 DT 加速效果。

诊断程序中的内存注册持续到显式关闭。源端收到读取完成确认之前不可复用；目标端传输未完成时
不可复用。取消不意味着 DMA 已停止，也不会自动释放内存。失败或完成状态不确定时，
保留注册直到所属进程终止；超时状态会继续轮询，卡住的诊断需由操作者终止。
这是内存所有权约束，不是自动恢复能力。

GPU 校验在本地 tensor 操作后记录 CUDA event，传输层在发布、覆盖和注销前等待
对应 event，不要求无关 stream 完成。调用方不传 event 时仍同步整个设备。

[Connector 所有权与引擎接口](docs/connector-contract_zh.md)
