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

值直接使用 YAML 布尔值、数字、字符串、列表或对象。省略选项或填写 `null` 即使用默认值。具体取值需与引擎镜像、模型及硬件匹配。

## 模型环境变量

通过 `spec.env` 为单个模型的运行进程设置环境变量：

```yaml
spec:
  env:
    OMP_NUM_THREADS: 8
    VLLM_LOGGING_LEVEL: DEBUG
    TOKENIZERS_PARALLELISM: false
```

值可以填写字符串、数字或布尔值，传给进程时统一转为字符串。保留 `"001"` 这样的文本时使用引号。`$(NAME)` 可以引用其他变量，无需调整声明顺序，但不能循环引用。

各 Pool 默认继承所属模型的 `spec.env`。填写 `modelPools[].env` 会整体替换该 Pool 的映射，`env: {}` 清空继承的自定义变量，省略或填写 `null` 则继续继承。

凭据使用 Secret 引用，不直接填写明文值：

```yaml
spec:
  env:
    AWS_ACCESS_KEY_ID:
      valueFrom:
        secretKeyRef:
          name: model-storage
          key: access-key-id
```

`valueFrom` 支持 Kubernetes 的 Secret、ConfigMap、Pod 字段和容器资源引用。上例需在模型所在命名空间创建 Secret `model-storage`，并提供 `access-key-id` 键。直接填写的值会随部署和实验配置保存。

变量按模型隔离，多节点副本的所有成员均生效。构建和独立模型下载仍使用各自的配置；启动、拓扑和缓存等平台管理的变量由 Foretoken 设置。

修改后执行 `foretoken deploy <配置目录>`，替换环境发生变化的模型实例。仅更新 Secret 或 ConfigMap 内容，要等新容器启动才生效；如需立即切换，改用新的引用并重新部署。

## 选择模型服务节点

通过 Kubernetes 节点 label，将整个服务或某个 Pool 固定到指定机器组。为组内每台节点设置相同的 key/value：

```bash
kubectl label node <node-a> workload-group=group-a --overwrite
kubectl label node <node-b> workload-group=group-a --overwrite
kubectl get nodes -L workload-group
```

然后在模型 YAML 中使用同一组 key/value：

```yaml
spec:
  nodeSelector:
    workload-group: group-a
```

`ModelService.spec.nodeSelector` 选择默认 Pool，`modelPools[].nodeSelector` 为单个 Pool 选择另一组节点。Kubernetes 会把模型服务和运行时准备 Pod 调度到带有该 label 的节点。

## 实例并发上限

要限制每个文本模型服务实例已接收但尚未完成的工作，包括引擎内排队的请求，在模型 YAML 中添加：

```yaml
spec:
  instanceAdmission:
    maxConcurrentRequests: 32
```

修改后，用 `foretoken deploy` 重新部署模型所在的配置目录。限额在线生效，已接收的工作继续执行；满额时停止接收新工作，直到占用降到上限以下。省略 `instanceAdmission` 表示不限流。

## 前缀缓存隔离

前缀缓存复用相同提示词前缀的计算结果。要按已认证的调用方隔离缓存，在 ModelService YAML 中添加：

```yaml
spec:
  prefixCacheIsolation: true
```

该设置统一用于模型的所有 Pool 和前端，覆盖 Chat Completions、文本补全、Responses 和 Messages，支持 vLLM 的文本与图片输入生成。

| 取值 | 缓存共享方式 |
| --- | --- |
| `true` | 按已认证的调用方隔离前缀缓存，忽略请求 body 中的 `cache_salt`。 |
| `false` | 不加盐，允许调用方共享相同前缀，忽略请求 body 中的 `cache_salt`。 |
| 省略或 `null` | 沿用请求 body 中的 `cache_salt`；body 省略该字段、填写 `null` 或空字符串时不加盐。 |

启用隔离需要接入认证网关。该设置仅改变缓存共享范围，前缀缓存开关和准入规则保持不变。

对包含模型和前端配置的目录执行 `foretoken deploy <配置目录>`。修改该设置会替换模型实例，并切换到独立的缓存域，持久化共享缓存也按模式分开。

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
| `enforce-eager` | `true` 时禁用计算图捕获；Encoder Pool 默认为 `true` |
| `enable-prefix-caching` | `true` 开启 KV 前缀缓存，`false` 关闭；Encoder Pool 固定为 `false` |
| `speculative-config` | 推测解码的原生配置字典 |
| `tensor-parallel-size` | 张量并行度（TP） |
| `pipeline-parallel-size` | 流水线并行度（PP） |
| `data-parallel-size` | 单个模型副本内的数据并行度（DP） |
| `prefill-context-parallel-size` | Prefill 上下文并行度（PCP） |
| `decode-context-parallel-size` | Decode 上下文并行度（DCP），复用已有 rank |

`nodes` 指定每个模型副本使用的 Kubernetes 节点数，`resources.requests.gpu.count` 是每个成员 Pod 申请的 GPU 数量。两者乘积必须等于 vLLM 的 TP × PP × DP × PCP，DCP 不增加 GPU 数。专家并行使用原生 `enable-expert-parallel`、`all2all-backend` 和 `enable-eplb` 参数。

模型副本可以跨节点运行，每个节点放置一个成员，按完整执行组启动、判断就绪和重启。`foretoken install` 自动准备 LeaderWorkerSet 控制器与 RDMA 分配，通信库从已分配设备中选择链路。跨节点使用的持久缓存需要所有成员均可访问。

P/D 和 E/P/D 各 Pool 可以在模型和引擎支持的组合内分别配置并行参数。PCP、DCP 的支持还取决于 attention backend。[EPD runtime 镜像](../examples/feature/encoder-prefill-decode/README_zh.md) 包含支持上下文并行的 Mooncake 传输实现：Prefill 与 Decode 需要使用匹配的 PCP/DCP 缓存布局，TP 大小需互为整数倍。

填写 `modelPools[].engineArgs` 时，它会整体替换该 Pool 继承的服务级原生参数。服务副本数与引擎内部的数据并行度分别配置。

开启专家并行（EP）后，各 DP rank 的 KV Cache 仍独立。单节点 EP 仅在实际通信后端需要时申请 RDMA 设备。

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

模型、tokenizer、节点数量和缓存传输分别通过 `spec.model`、`spec.tokenizer`、`nodes`、`kvCache` 和 `ecProfile` 配置，Foretoken 据此设置启动参数。启用运行时缓存时，原生 profiler 配置由性能剖析功能管理；mcTracer 还会选择对应的 worker。多节点副本使用 `mp` executor。

其余原生选项交给 vLLM，包括 `ec-manager-config`，以及未使用 mcTracer 时的自定义 worker。完整选项见 [vLLM 参数文档](https://docs.vllm.ai/en/latest/configuration/engine_args/)。
