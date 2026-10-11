<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken Draft/Target 服务

[English](README.md) | 简体中文

将用户指定的 Draft、Target 模型分别运行在不同 GPU 或主机上。前端每轮选择 DT worker，
协调候选生成和验证，只向用户输出 Target 确认的结果。Mooncake 在 GPU Worker 之间
传输 Draft 的完整提议分布；HTTP 传递候选 token 和张量描述符。两端各自管理调度和 KV。

下面直接启动两个角色服务，再通过 [Rust 前端示例](docs/frontend-workflow.md)发送请求。

## 启动两个角色

两台主机均需安装原版 vLLM `0.31.0`，或仓库固定的
沐曦版本组合（`vllm==0.30.0.dev0`、`vllm-metax==0.29.0.dev0`），
并准备 Python 3.10+、兼容的 PyTorch/Mooncake wheel 和可用的 GPU/RDMA 访问。
model-server 镜像已在引擎环境中安装本包。独立启动时，在该环境执行下方安装命令；
vLLM 和匹配加速卡的 Mooncake wheel 需预先安装。引擎兼容要求见
[集成说明](docs/mrv2-integration.md)。

沐曦需使用包含补丁的[源码运行环境](../../deploy/inference-engines/vllm-metax/)，
其中包含 Model Runner V2 所需的 FlashAttention 序列长度修复。
应显式设置 `VLLM_USE_V2_MODEL_RUNNER=1`，因为平台默认使用旧版 Runner。
RDMA 需要兼容 MACA 的 Mooncake 构建；源码镜像已设置 `MC_MACA_HOST_TRANSPORT=1`。
沐曦贪心输出可能因 batch 形状变化而与独立 Target 解码不同。

使用已加载的 `nvidia-peermem` 内核模块时，两端进程设置 `WITH_NVIDIA_PEERMEM=1`；
否则 Mooncake 默认的 DMA-BUF 路径需要相应驱动支持。容器还需具备 GPU/RDMA 设备访问、
RDMA 用户态库和足够的锁定内存额度。

将 `DRAFT_MODEL`、`TARGET_MODEL` 设为模型名称或本地路径。两模型必须具有相同的词表
大小和 token ID 含义。`TOKENIZER` 指向双方共同使用的 tokenizer，通常来自 Target。
`DRAFT_IP`、`TARGET_IP` 分别为两台主机的本地可路由地址，不含端口。

两端都在仓库根目录安装：

```bash
python -m pip install ./data-plane/dt-plugin
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
对应请求，其他请求仍可执行。
命令接受 vLLM 原有的模型、显存及 batch 参数。
`--draft-token-budget` 设置每轮候选上限，默认 3；`--port` 设置 HTTP 端口，默认 19100；
`--rdma-nic` 可指定 HCA，不设置时由 Mooncake 从可见设备中选择。
HTTP 端口和 Mooncake 动态分配的握手端口需要互通。

角色启动器在 NVIDIA 上默认设置 vLLM 原生 `VLLM_BATCH_INVARIANT=1`，在沐曦上默认设为 `0`，
并保留显式设置的环境变量。NVIDIA 的默认值用于减少 batch 形状引起的数值差异。
该模式文档支持的硬件范围是计算能力 8.0 及以上的 NVIDIA GPU，模型和
attention 后端也必须支持它。启动前显式设置 `VLLM_BATCH_INVARIANT=0` 可关闭；关闭后
贪心输出可能随 batch 组合变化。与独立 Target 对比时应使用相同设置。

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
两端都需要访问 Target 和 Draft checkpoint。现有 Rust frontend 自动协调 Target
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

当前每个 inference round 使用一个 Draft 和一个 Target，处理文本输入及线性候选。
显式 Target migration 需要在两个 NVIDIA Target 启用 RDMA 和 `--enable-kv-migration`，
再使用 [batch example](docs/frontend-workflow.md#move-a-request-between-targets)。
migration 目前要求 greedy sampling、相同 model/tokenizer identity，以及兼容的
full-attention KV layout。公开 scheduler 保留当前 Target，不会自动迁移 request。
尚未实现候选树、串联验证、多模态、P/D 组合或 KV offload。

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
