<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken DT 角色与传输

[English](README.md) | 简体中文

本包提供独立的贪心 Draft、Target 角色服务，以及 Mooncake tensor 校验命令。
Target 依赖独立 vLLM `feat/external-speculation` 扩展，详见[接入契约](docs/mrv2-integration.md)；
仓库固定版本的引擎不能运行该角色。安装不会修改 vLLM，也不会注册 `vllm serve` 模式。
[Rust 前端协调流程](docs/frontend-workflow.md) 提供可运行的 token 输入示例；
也支持通过现有 Router 接入公共 API。[实验性 Kubernetes 示例](../../examples/draft-target/README_zh.md)
使用控制器完成角色部署、自动发现和有时限的排空；平台运行镜像必须包含独立引擎扩展。

## 启动角色

使用 Python 3.10+，环境中需已有对应的 vLLM 引擎、PyTorch、FastAPI、Uvicorn、
AnyIO 和 Pydantic 2。在仓库根目录执行：

```bash
python -m pip install --no-deps ./data-plane/dt-plugin
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role draft --model "$DRAFT_MODEL"
```

Target 主机安装独立引擎扩展后运行：

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role target --model "$TARGET_MODEL"
curl http://127.0.0.1:19100/status
```

两模型须为 token ID 含义一致的兼容文本模型。当前支持贪心线性候选、单 Target worker；
Target 使用 eager 执行，关闭本地异步调度。等待候选只暂停对应请求。
命令接受标准 vLLM 模型、显存和 batching 参数；`--draft-token-budget` 设置候选上限，默认 3。
通过 `--host`、`--port` 向可信部署网络开放访问，默认只监听本机。
这是内部服务，不提供鉴权或公共 OpenAI 接口。

前端需驱动[角色协议](docs/role-protocol.md)。Draft 每轮提交普通 vLLM 请求并复用 prefix cache，
不自行维护跨轮存活的 Draft KV 游标。关闭所属控制流会取消会话。
`POST /drain` 拒绝新会话，但允许已有会话继续；停服前需关闭已有控制流。

少量贪心候选 token 通过 HTTP 控制消息传输。该推理路径不传 KV，也未通过 Mooncake 传候选。
下面的 tensor 传输独立可用；随机采样候选、携带 tensor 的算法、多模态、PD 组合和自动扩缩容
不在当前角色实现范围内。两角色跑通不代表有加速收益。

## 运行两机校验

使用两台具有 Python 3.10 或以上版本、可用 RDMA 的 Linux 服务器，确保控制端口和 Mooncake RPC 端口可达，
并使用引擎环境中的 PyTorch 和 Mooncake wheel。传输适配针对仓库固定的 Mooncake
提交 `719735896c86b56fabec6cf3e825fb2ea640597a`。使用平台提供、与加速卡匹配的
wheel；安装本包不会替换该 wheel，也不会安装 vLLM。GPU 内存注册需要硬件与驱动支持。
A100 跨机验证使用 `mooncake-transfer-engine-cuda13==0.3.13.post1` 和 PyTorch
`2.11.0+cu130`。CUDA 12 的 Mooncake wheel 不能直接当作 CUDA 13 wheel 使用。

使用已加载的 `nvidia-peermem` 内核模块时，两端进程设置 `WITH_NVIDIA_PEERMEM=1`；
否则 Mooncake 默认的 DMA-BUF 路径需要相应驱动支持。容器还需具备 GPU/RDMA 设备访问、
RDMA 用户态库和足够的锁定内存额度。

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

内存注册持续到显式释放。源端收到读取完成确认之前不可复用；目标端传输未完成时
不可复用。取消不意味着 DMA 已停止，也不会自动释放内存。失败或完成状态不确定时，
保留注册直到所属进程终止；超时状态会继续轮询，卡住的诊断需由操作者终止。
这是内存所有权约束，不是自动恢复能力。

GPU 校验在本地 tensor 操作后记录 CUDA event，传输层在发布、覆盖和注销前等待
对应 event，不要求无关 stream 完成。调用方不传 event 时仍同步整个设备。
tensor 校验成功不代表投机解码正确或有性能收益。
