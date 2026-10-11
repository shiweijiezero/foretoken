<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Draft/Target 服务

[English](README.md) | 简体中文

将用户指定的 Draft、Target 模型分别运行在不同 GPU 或主机上。前端每轮选择 DT worker，
协调候选生成和验证，只向用户输出 Target 确认的结果。Mooncake 在 GPU Worker 之间
传输 Draft 的完整提议分布；HTTP 传递候选 token 和张量描述符。两端各自管理调度和 KV。

通过现有 Controller、Router 部署请使用 [Kubernetes 示例](../../examples/draft-target/)。
下面直接启动两个角色服务，再通过 [Rust 前端示例](docs/frontend-workflow.md)发送请求。

NVIDIA 源码构建中，`make image-model-server` 会构建固定版本的 CUDA 13 引擎并安装本插件，
包含 vLLM 子进程发现所需的入口。`foretoken install -e .` 默认使用同一运行环境。
主机需支持 CUDA 13；显式覆盖 `INFERENCE_ENGINE_IMAGE` 时，应提供下述兼容引擎和 Mooncake 环境。
沐曦沿用独立的运行时构建，兼容要求见下文。

## 每轮组 batch 和选 DT

公开 text/chat request 进入 per-model ready queue。scheduler 每轮将 ready 的
request 组 batch，再选择兼容的 DT。
[batch frontend 示例](docs/frontend-workflow.md#run-per-round-batches-through-router)
也提供 round API，供自定义 scheduler 使用。request 可以携带已确认的
prefix 切换 Draft。request 记录当前 Target 位置；普通 round 复用该 Target，
也可以在 round 之间显式调用 migration，将 KV 移到另一个兼容的 Target。
vLLM 负责本地 GPU batching。每个 request 独立返回 ready queue，不必等其他
request 的 prefill。output backpressure 和取消也按 request 处理。当前策略立即
执行 ready 的工作，没有 SLO 或 migration cost 策略。

## 观察 DT 执行

batch example 会输出每轮耗时、proposed/accepted/committed token 数和 transfer
数据。role `/status` 提供 native ready、waiting、remote-waiting 工作量和 KV 容量。
上层 scheduler 也可以查询 request 当前的 Target 位置和 KV 占用，作为 placement
决策的输入。显式 migration 会记录 checkpoint export 耗时、传输字节数和 receiver
read 耗时；目前没有 migration cost estimator。
具体字段与统计口径见 [measurement 定义和 API](docs/role-protocol.md#observations)。
frontend `/metrics` 提供 DT 汇总指标；request、round 和 worker 的对应关系保留在
trace 和每轮返回的记录中。

## 启动两个角色

两台主机均需安装原版 vLLM `0.30.1rc1.dev194+g3b4566c5c`，或仓库固定的
沐曦版本组合（`vllm==0.30.0.dev0`、`vllm-metax==0.29.0.dev0`），
并准备 Python 3.10+、兼容的 PyTorch/Mooncake wheel 和可用的 GPU/RDMA 访问。
环境还需包含 FastAPI、Uvicorn、AnyIO、Pydantic 2 和 HTTPX。
本包通过原生类选择接口和少量进程内运行时 patch 接入，不修改已安装的 vLLM 文件，
不依赖独立引擎分支，也不安装 vLLM 或注册 `vllm serve` 模式。
适配层限定上述引擎版本，详见[集成说明](docs/mrv2-integration.md)。

沐曦需使用包含补丁的[源码运行环境](../../deploy/inference-engines/vllm-metax/)，
其中包含 Model Runner V2 所需的 FlashAttention 序列长度修复。
应显式设置 `VLLM_USE_V2_MODEL_RUNNER=1`，因为平台默认使用旧版 Runner。
RDMA 需要兼容 MACA 的 Mooncake 构建；源码镜像已设置 `MC_MACA_HOST_TRANSPORT=1`。
生成成功不能证明 GPU 直达传输、跨机运行或性能提升。
沐曦贪心输出可能因 batch 形状变化而与独立 Target 解码不同。

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

每个角色使用一个 GPU Worker、eager 执行和本地同步调度。等待远端候选只暂停
对应请求，其他请求仍可执行。NVIDIA role 在报告 ready 前执行 native sampling JIT
warmup；Draft 还会预热导出 proposal probabilities 所用的 sampling 路径。
首次编译可能延长启动时间；未覆盖的 kernel 变体仍可能在推理时编译。
命令接受 vLLM 原有的模型、显存及 batch 参数。
`--draft-token-budget` 设置每轮候选上限，默认 3；`--port` 设置 HTTP 端口，默认 19100；
`--rdma-nic` 可指定 HCA，不设置时由 Mooncake 从可见设备中选择。
HTTP 端口和 Mooncake 动态分配的握手端口需要互通。

角色启动器在 NVIDIA 上默认设置 vLLM 原生 `VLLM_BATCH_INVARIANT=1`，在沐曦上默认设为 `0`，
并保留显式设置的环境变量。NVIDIA 的默认值用于减少 batch 形状引起的数值差异。
该模式文档支持的硬件范围是计算能力 8.0 及以上的 NVIDIA GPU，模型和
attention 后端也必须支持它。启动前显式设置 `VLLM_BATCH_INVARIANT=0` 可关闭；关闭后
贪心输出可能随 batch 组合变化。与独立 Target 对比时应使用相同设置。
当前未测量这一设置的性能成本，也不承诺所有配置都能逐 token 完全一致。

角色启动器会将 `foretoken_dt` 加入继承的 `VLLM_PLUGINS` 白名单，确保引擎子进程加载必要的钩子。
沐曦环境还会加入平台的 `metax` 插件。

支持 `temperature`、`top_p`、`top_k` 和 Target 的 `seed`。
Draft 使用独立的随机数；固定 Target seed 不保证不同候选轮次或 batch 组合产生相同序列。

前端拒绝非默认的重复、频率和存在惩罚，包括从模型 `generation_config.json` 继承的值。
若模型默认开启重复惩罚，且你希望关闭它，应在请求中显式设置 `"repetition_penalty": 1.0`。
当前 DT 路径不能保留非默认的惩罚行为，也不会静默忽略它。

不使用 RDMA 时，两端都省略 `--rdma-host`。此时仅支持 `temperature: 0`，
能力标识为 `greedy_token_ids`，候选通过 HTTP 传递；随机采样请求会在准入前被拒绝。

## 在 NVIDIA 上运行神经 Draft

使用上述 NVIDIA 环境和兼容的未量化文本 Target。以 DFlash 为例，选择 `DFlashDraftModel` checkpoint。
两端使用相同的原生 speculative 配置。例如，设置 `TARGET_MODEL=Qwen/Qwen3-8B`、
`TOKENIZER=Qwen/Qwen3-8B`，并在两端设置：

```bash
SPEC_CONFIG='{"method":"dflash","model":"z-lab/Qwen3-8B-DFlash-b16","num_speculative_tokens":15}'
```

Draft 主机运行：

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role draft \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --speculative-config "$SPEC_CONFIG" \
  --host 0.0.0.0 --rdma-host "$DRAFT_IP"
```

Target 主机运行：

```bash
VLLM_USE_V2_MODEL_RUNNER=1 foretoken-dt-role --role target \
  --model "$TARGET_MODEL" --tokenizer "$TOKENIZER" \
  --speculative-config "$SPEC_CONFIG" \
  --host 0.0.0.0 --rdma-host "$TARGET_IP"
```

同机运行时，两进程应选择不同 GPU 和 HTTP 端口。候选上限来自 `num_speculative_tokens`。
Draft 主机需要访问 Target checkpoint，以加载共享 embedding/output 权重，但不会把
Target 主干加载到 GPU。Target 保留 hidden states，Draft 在本地请求入队前通过 Mooncake
拉取已确认前缀的特征。现有 Rust frontend 自动完成交换。两端 `/status` 的
`context_profile` 必须一致；使用本地 checkpoint 时，两端路径需标识同一模型对。

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

此路径支持 greedy/random sampling、原生 chunked prefill，以及每轮重新绑定 Draft。
Target prefix caching 被禁用，因为 KV 命中会跳过 Draft 需要的特征计算。DFlash 和 DSpark 保留
Draft 本地 prefix caching；EAGLE/MTP Draft 禁用该缓存，因为其 KV 还依赖后一位 token，
而该 token 可能位于参与哈希的 prompt 之外。每次提议都传输完整的已计算特征前缀，
长上下文会产生重复传输和 prefill 开销。

GPU 验证覆盖 Qwen3-8B 配 DFlash/EAGLE3/DSpark，以及 MiMo-7B-Base MTP。EAGLE 使用相同的
自回归适配路径，但尚未单独验证 checkpoint。尚未启用多模块 MTP、Gemma4 Target KV
共享、HC residual、Target 共享稀疏注意力索引、DFlash2、LiLiCorr、沐曦神经 Draft，
以及携带特征的 Target migration。生成正确不代表已有加速收益。

## 停止服务与支持范围

`POST /drain` 关闭新请求准入，允许已有会话完成。关闭前端输出流可取消请求。
正常停服前，应等待 `/status` 中的 `active_sessions` 和 `retained_artifacts` 都归零。
GPU 缓冲区注册保留到角色关闭。特征缓冲区按几何增长的行容量复用，其他缓冲区按精确
形状复用，因此会话结束后显存可能仍保持对应容量的并发高水位。取消 Draft session 会回收 Target 尚未 claim 的发布。
已 claim 但未收到 ACK 的发布或完成状态不确定的 DMA 可能保留内存直到进程终止；
当前不提供透明的对端故障恢复。
artifact claim 是内部协议变更，需一起更新 frontend 和两端 role。

当前每个 inference round 使用一个 Draft 和一个 Target，处理文本输入及线性候选。
显式 Target migration 需要在两个 NVIDIA Target 启用 RDMA 和 `--enable-kv-migration`，
再使用 [batch example](docs/frontend-workflow.md#move-a-request-between-targets)。
migration 目前要求 greedy sampling、相同 model/tokenizer identity，以及兼容的
full-attention KV layout。公开 scheduler 保留当前 Target，不会自动迁移 request。
尚未实现候选树、串联验证、多模态、P/D 组合或 KV offload。
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
