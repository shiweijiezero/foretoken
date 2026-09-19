<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 推理参数

[English](inference-parameters.md) | 简体中文

通过 `ModelService.spec.engineArgs` 配置所选引擎，参数名沿用原生名称，不写 `--`：

```yaml
spec:
  model: Qwen/Qwen2.5-0.5B-Instruct-AWQ
  backend: vllm
  engineArgs:
    quantization: awq
    dtype: half
    max-model-len: 8192
    tensor-parallel-size: 1
    gpu-memory-utilization: 0.85
```

值直接使用 YAML 布尔值、数字、字符串、列表或对象。未填写的选项沿用引擎默认值，`null` 表示不传该原生选项。具体取值需与引擎镜像、模型及硬件匹配。

## vLLM 常用参数

| 引擎参数 | 用途 |
| --- | --- |
| `max-model-len` | 输入和输出合计的最大 token 数 |
| `dtype` | 模型计算精度 |
| `quantization` | 权重量化方法 |
| `kv-cache-dtype` | KV Cache 精度，与权重量化分别配置 |
| `gpu-memory-utilization` | 每个引擎实例可使用的显存比例 |
| `max-num-seqs` | 每轮调度的最大序列数 |
| `max-num-batched-tokens` | 每轮调度的最大 token 数 |
| `enforce-eager` | `true` 时禁用计算图捕获 |
| `speculative-config` | 推测解码的原生配置字典 |
| `tensor-parallel-size` | 张量并行度（TP） |
| `pipeline-parallel-size` | 流水线并行度（PP） |
| `data-parallel-size` | 单个模型副本内的数据并行度（DP） |
| `prefill-context-parallel-size` | Prefill 上下文并行度（PCP） |
| `decode-context-parallel-size` | Decode 上下文并行度（DCP），复用已有 rank |

`nodes` 指定每个模型副本使用的 Kubernetes 节点数，`resources.requests.gpu.count` 是每个成员 Pod 申请的 GPU 数量。两者乘积必须等于 vLLM 的 TP × PP × DP × PCP，DCP 不增加 GPU 数。专家并行使用原生 `enable-expert-parallel`、`all2all-backend` 和 `enable-eplb` 参数。

聚合式副本可以跨节点运行，每个节点放置一个成员，按完整执行组启动、判断就绪和重启。`foretoken install` 自动准备 LeaderWorkerSet 控制器与 RDMA 分配，通信库从已分配设备中选择链路。跨节点使用的持久缓存需要所有成员均可访问。多节点执行目前要求 PCP=1；分离式服务仍要求单节点、单 rank。

填写 `modelPools[].engineArgs` 时，它会整体替换该 Pool 继承的服务级原生参数。服务副本数与引擎内部的数据并行度分别配置。

开启 EP 后，attention 可以按 TP × DP 执行，路由专家则分布在对应的 EP 组中。共享专家不代表共享 attention 的 KV Cache：路由会选择模型执行组及其中的 DP rank，按各 rank 的前缀缓存观测做选择。某个 rank 的 KV 事件不可用或中断时，其缓存位置按未知处理，不计为命中，也不影响其他 rank 的观测。负载评分从同一份遥测快照读取各 DP rank 的运行请求数、等待请求数和 KV 使用率，执行组汇总继续供扩缩容使用。启用 Mooncake Store 时，共享前缀查询由所选 rank 的原生连接器检查全部必需缓存分片。

## 推测解码

将完整的原生字典写在一起：

```yaml
spec:
  model: Qwen/Qwen3-0.6B
  backend: vllm
  engineArgs:
    speculative-config:
      method: ngram
      num_speculative_tokens: 2
      prompt_lookup_max: 4
```

方法和子字段沿用 vLLM。需要草稿权重时，`model` 可填写 Hub 模型 ID 或容器内可见的绝对目录，由 vLLM 下载、加载和缓存。`spec.source: modelscope` 同时适用于主模型和草稿模型的 Hub ID。

## 平台管理的选项

模型标识、启动端点、传输连接器和性能剖析由 Foretoken 管理，其余原生选项交给所选引擎解释。完整选项见 [vLLM 参数文档](https://docs.vllm.ai/en/latest/configuration/engine_args/)；当前已实现的 backend 为 vLLM。
