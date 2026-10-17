<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Draft/Target 服务

[English](README.md) | 简体中文

将 Draft、Target 分别运行在不同 GPU 或主机上。Foretoken Router 为每个请求选择
一个兼容的 Draft 和 Target。选中的 Target 管理确认前缀与轮次推进，各引擎原生
Scheduler 管理 token budget、KV 和实际组批。Frontend 只返回 Target 确认输出。
固定 N:M worker 集合中的多个请求独立推进。

下面直接启动两个角色服务，再通过 [Rust 前端示例](docs/frontend-workflow.md)发送请求。

## 启动两个角色

两台主机均需安装原版 vLLM `0.31.0`，或仓库固定的
沐曦版本组合（`vllm==0.30.0.dev0`、`vllm-metax==0.29.0.dev0`），
并准备 Python 3.10+、兼容的 PyTorch 和可用的 GPU 访问。
model-server 镜像已在引擎环境中安装本包。独立启动时，在该环境执行下方安装命令；
vLLM 和匹配加速卡的运行环境需预先安装。引擎兼容要求见
[集成说明](docs/mrv2-integration.md)。

沐曦需使用包含补丁的[源码运行环境](../../deploy/inference-engines/vllm-metax/)，
其中包含 Model Runner V2 所需的 FlashAttention 序列长度修复。
应显式设置 `VLLM_USE_V2_MODEL_RUNNER=1`，因为平台默认使用旧版 Runner。
RDMA 需要兼容 MACA 的 Mooncake 构建；源码镜像已设置 `MC_MACA_HOST_TRANSPORT=1`。
严格的贪心对照还需要下文所述的 batch-invariant 配置。

将 `DRAFT_MODEL`、`TARGET_MODEL` 设为模型名称或路径。两模型必须具有相同词表
大小和 token ID 含义；`TOKENIZER` 指向共同 tokenizer，通常来自 Target。
默认 auto 可通过 ZMQ TCP 工作，无需 Mooncake、RDMA 设备或共享 socket 目录；tensor 端口自动分配。

两端都在仓库根目录安装：

```bash
python -m pip install ./data-plane/dt-plugin
```

Draft 主机运行：

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role draft \
  --model "$DRAFT_MODEL" --tokenizer "$TOKENIZER" \
  --host 0.0.0.0
```

Target 主机运行：

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role target \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --host 0.0.0.0
curl http://127.0.0.1:19100/status
```

`/status` 应返回正确的模型和角色、`accepting: true`，以及
`candidate_format: "token_ids_log_probs"`、`tensor_transports: ["zmq"]`、`protocol_version: 9`（没有可用的 RDMA 或 GPU peer 路径时）。前端选中的两个角色必须使用相同格式。
使用两端 HTTP 地址启动前端；角色服务仅提供内部 API，没有公共 OpenAI 接口或鉴权。

每个角色使用一个 GPU Worker、eager 执行和本地同步调度。等待远端候选只暂停
对应请求，其他请求仍可执行。
命令接受 vLLM 原有的模型、显存及 batch 参数。
Target 的 `--draft-tokens` 设置每轮默认候选数，默认 3；
`--max-candidate-tokens` 设置引擎启动时的候选容量，省略时从默认候选数推导。
例如在 Target 命令中添加 `--draft-tokens 3 --max-candidate-tokens 8`，可为最多
8 个候选准备容量，当前每轮请求 3 个。默认值必须落在容量内；最后一轮按剩余输出长度截短。

普通自回归 Draft 不配置固定候选上限，按 Target 请求的数量执行，仍受原生上下文长度和
资源约束。这两个选项仅用于 Target。升级时同时更新 Frontend 和两端角色；协议 v9
公布已初始化的 tensor 传输能力供绑定时选择，并分别表达轮次候选策略和引擎容量。
`--port` 设置 HTTP 端口，默认 19100；
同机角色应选择不同 GPU 和端口。Frontend 与选中的 Target 必须能访问角色 HTTP 地址。

启动器保留 vLLM 原生 `VLLM_BATCH_INVARIANT` 的默认值（关闭）。
严格比较贪心 token IDs 时，应为两个 DT 角色和独立 Target 基线显式设置
`VLLM_BATCH_INVARIANT=1`。该模式要求兼容的模型和 attention 后端；NVIDIA GPU
需要计算能力 8.0 及以上。固定的沐曦源码构建包含适配 64 KiB shared memory 上限的
原生 GEMM 小 tile，并让 PyTorch linear 使用固定归约内核，避免 CUDA 专用 BLAS 控制。
已验证 BF16 Qwen3-4B、budget=1 和 3 的 verification。
关闭 batch invariance 时，batch 形状变化可能改变 logits 排序和贪心输出。
token IDs 不一致仍算对照失败，不能仅凭 logits 接近认定正确。具体范围见
[验证边界](docs/mrv2-integration.md#greedy-validation)。

token-only 路径要求 `temperature: 0`，随机采样在 admission 前拒绝。tensor
adapter 支持 `temperature`、`top_p`、`top_k` 和 Target `seed`。Draft 使用独立随机数；
固定 Target seed 不保证不同候选轮次或 batch 组合产生相同序列。

前端拒绝非默认的重复、频率和存在惩罚，包括从模型 `generation_config.json` 继承的值。
若模型默认开启重复惩罚，且你希望关闭它，应在请求中显式设置 `"repetition_penalty": 1.0`。
当前 DT 路径不能保留非默认的惩罚行为，也不会静默忽略它。
DT adapter 暂不支持按调用方隔离前缀缓存，因此拒绝 `prefixCacheIsolation` 配置和
请求中的显式 `cache_salt`。

## 自动选择 tensor 传输

默认的 `--tensor-transport auto` 提供 ZMQ TCP、已分配的 Mooncake RDMA，
以及设备权限和 P2P 能力允许时的 MACA GPU IPC。托管部署自动注入 Pod IP 和 RDMA 分配，客户不必填写 tensor 地址
或端口。独立启动时使用 RDMA 地址或 `--host` 地址；监听所有网卡时沿用 vLLM 的 `VLLM_HOST_IP`
或网卡地址选择。两端需要能访问公布的地址和动态分配的 TCP 端口。

Router 检查是否有兼容的已初始化 transport。Target 优先选择同机且双向 peer
访问均被授予的 GPU IPC，然后是共同 RDMA，最后是 ZMQ；整个 request 保持该选择。`DT binding` 日志记录
request、两端地址和所选方式。这是能力规则，不是已经测量过的性能最优规则。
在途传输失败后不会换路重放，原 owner 继续承担完成和清理责任。

ZMQ 经主机内存传输概率和特征，包含 D2H/H2D 拷贝。TCP 可用于不同 Pod 或主机，
无需共享文件系统。若部署明确提供同机共享 socket 目录，可分别指定
`--zmq-endpoint ipc:///tmp/foretoken-draft.sock` 和
`--zmq-endpoint ipc:///tmp/foretoken-target.sock`。路径必须唯一，且两端 Worker
通过相同绝对路径访问。IPC 是明确的部署选项；auto 不会假设同节点容器共享目录。
两种 ZMQ socket 都不提供 GPU P2P。

MACA GPU IPC 复用 Mooncake 的 `maca` 后端，传输设备 tensor 时不经过插件的
主机中转。两个容器都需有权访问本次服务分配的 GPU 对，`CUDA_VISIBLE_DEVICES`
仍选择各自的计算 GPU。设备访问权限不等于使用授权；运维只能授予已分配给该服务的
peer。Foretoken 不扩大设备访问权限，也不更改 GPU 资源分配。当前每 Pod 一卡的
隔离方式下，若部署没有额外授予必要的 peer 访问，auto 会保留 RDMA/ZMQ。
仅共享 host IPC namespace 不足以启用这条路径，本次也不会开启它。

Worker 通过 `gpu_ipc: {host_id, device_id, peer_devices}` 公布能力，路由和绑定检查
同机及双向 peer 访问。能力查询使用短暂的独立元数据进程，不改变计算 Worker 的
设备可见性。`--tensor-transport gpu-ipc` 可强制该路径，缺少 peer 权限时启动失败。
当前只接入 MACA，未启用 NVIDIA CUDA IPC/NVLink；实际互联利用率和性能需测量，
能力字段不代表已经使用 NVLink 硬件链路。

独立运行 RDMA 时，两端安装兼容 Mooncake wheel，提供 GPU/RDMA 设备、用户态库
及足够锁定内存，分别添加 `--rdma-host "$DRAFT_IP"`、`--rdma-host "$TARGET_IP"`。
auto 将同时提供 `rdma` 和 `zmq`。`--rdma-nic` 可指定 HCA；加载 `nvidia-peermem`
时设置 `WITH_NVIDIA_PEERMEM=1`，否则 DMA-BUF 路径需驱动支持。auto 模式下，
Mooncake 包缺失或 RDMA 初始化返回失败会记录原因，并从能力中移除 RDMA；强制 RDMA 时仍
启动失败。能力筛选发生在模型 tensor 发布和传输之前，其他错误不会被吞掉。仅检测到网卡
不算 ready；还检查实际 Worker GPU 注册。RDMA 绑定在 Target prefill 前等待通信连接
拥有的双向 GPU 内容校验；同一实际 transport 实例对复用成功结果，实例变化或连接
失败后旧结果失效，见[绑定验证](docs/connector-contract_zh.md#rdma-绑定验证)。
初始化成功不等于通过 GPUDirect 验证。

运维和对照实验可通过 `--tensor-transport rdma` 或 `--tensor-transport zmq`
强制指定方式。两端使用 `--tensor-transport none` 可运行 greedy token-only 路径，
该模式不支持神经 Draft 或随机采样。状态中的 `tensor_transports` 公布能力列表，
token-only 时为空；升级时同时更新 Frontend 和两端角色。RDMA、跨机 TCP 及 GPU
性能仍需分别通过真实部署验证。

## 运行神经 Draft

使用上述受支持环境和兼容的未量化文本 Target，原生引擎需要实现所选 Draft 方法。
以 DFlash 为例，选择 `DFlashDraftModel` checkpoint。
两端通过原生 speculative 配置标识相同的 Draft 方法和模型对。例如，设置 `TARGET_MODEL=Qwen/Qwen3-8B`、
`TOKENIZER=Qwen/Qwen3-8B`，并在两端设置：

```bash
SPEC_CONFIG='{"method":"dflash","model":"z-lab/Qwen3-8B-DFlash-b16","num_speculative_tokens":15}'
```

同机使用自动 ZMQ TCP 时，在 GPU 0 启动 Draft：

```bash
CUDA_VISIBLE_DEVICES=0 VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role draft \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --speculative-config "$SPEC_CONFIG" \
  --port 19100
```

在 GPU 1 启动 Target：

```bash
CUDA_VISIBLE_DEVICES=1 VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role target \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --speculative-config "$SPEC_CONFIG" \
  --port 19101
```

跨主机执行时，两端添加 `--host 0.0.0.0`。RDMA 可用时，再分别添加
本机的 `--rdma-host` 并满足上述前提。原生容量来自 `num_speculative_tokens`，Target 通过 `--draft-tokens` 设置轮次候选数，默认 3。
Draft 计算原生候选块，按请求长度发布前缀及对应的概率行。
两端都需要访问 Target 和 Draft checkpoint。Target 与 Draft 直接协调
hidden states 的传输。两端 `/status` 的 `context_profile` 必须一致；使用本地 checkpoint 时，两端路径需标识同一模型对。

相同的角色命令也接受 EAGLE/EAGLE3 和单模块 MTP 配置。同样使用 Qwen3-8B Target 时，
EAGLE3 配置为：

```bash
SPEC_CONFIG='{"method":"eagle3","model":"AngelSlim/Qwen3-8B_eagle3","num_speculative_tokens":3}'
```

同样使用 Qwen3-8B Target 时，DSpark 配置为：

```bash
SPEC_CONFIG='{"method":"dspark","model":"deepseek-ai/dspark_qwen3_8b_block7","num_speculative_tokens":7}'
```

DSpark 复用原生 Markov 采样，按 Target 词表顺序传输每个候选的条件概率。
使用 `Qwen3DSparkModel` checkpoint，并保持固定长度验证
（`enable_adaptive_verification=false`，原生默认值）。远端 Draft 尚未接入基于
confidence 的自适应验证。

使用 MiMo MTP 时，把 `TARGET_MODEL` 和 `TOKENIZER` 都设为 `XiaomiMiMo/MiMo-7B-Base`，
两端角色命令均添加 `--trust-remote-code` 以加载其配置类，并使用：

```bash
SPEC_CONFIG='{"method":"mtp","num_speculative_tokens":3}'
```

神经 Draft 支持 greedy/random sampling 和 chunked prefill。Target 禁用 prefix caching，
EAGLE/MTP 的 Draft 也禁用该缓存。每轮传输完整的已计算特征前缀，因此长上下文开销较大。
支持的模型变体与限制见[集成说明](docs/mrv2-integration.md#neural-draft-context)。

## 停止服务与支持范围

`POST /drain` 关闭新请求准入，允许已有会话完成。关闭前端输出流可取消请求。
正常停服前，应等待 `/status` 中的 `active_sessions` 和 `retained_artifacts` 都归零。
已注册的缓冲区保留到停服以供复用。对端故障或完成状态不确定的 DMA 可能需要终止进程，
详见[内存与取消契约](docs/connector-contract_zh.md#内存取消与排空)。

每个 Target pool 声明兼容的 Draft pools，Router 通过配置的路由 pipeline 一次
选择绑定。本次交付每请求一个 Draft、每轮一条候选链，由 Target 配置默认候选数。
多 Draft 协作、树、级联验证、多模态输入、P/D 组合、KV offload 和自动迁移尚未实现。
[checkpoint/resume 协议](docs/role-protocol.md#target-kv-checkpoint-and-resume)
仍可用于手动诊断，要求 NVIDIA RDMA
（`--tensor-transport rdma --enable-kv-migration`）、greedy sampling、相同 model/tokenizer identity
与兼容的 full-attention KV layout。

## 运行两机校验

在与角色服务相同的引擎和 RDMA 环境中执行诊断。

将 `PRODUCER_IP` 设置为生产端可路由 IP，`CONSUMER_IP` 设置为消费端可路由 IP，
均不带端口。在生产端运行：

```bash
foretoken-dt-transfer serve --host "$PRODUCER_IP" --device cuda:0
```

看到 `listening` 后，在消费端运行：

```bash
foretoken-dt-transfer read --host "$CONSUMER_IP" --peer "$PRODUCER_IP" --device cuda:0
```

成功后消费端输出 `verified: true`、字节数、设备和传输耗时，两个进程退出。
控制通信使用 TCP 19090，需允许该端口和 Mooncake 动态握手端口在两台主机间互通。

通过 `--nic` 选择 RDMA 设备，通过两端的 `--port` 修改控制端口；生产端的
`--elements` 改变数据大小。生产端 `--iterations 4` 在同一组源/目标注册缓冲区上
连续发布四轮，每轮改变内容以检查旧数据误用；输出 `bytes` 是每轮字节数，
`transfer_seconds` 是所有轮次读取耗时之和。`--device cpu` 可诊断主机内存传输，但不能验证 GPU
直达。控制消息假设来自部署网络中的可信对端。

诊断明确请求 RDMA。需结合 Mooncake 日志和设备/网络计数器确认 GPUDirect RDMA，
`requested_transport` 不足以判断实际路径。耗时包含建连和轮询开销。
传输卡住时，应终止两端诊断进程后再复用其内存。
