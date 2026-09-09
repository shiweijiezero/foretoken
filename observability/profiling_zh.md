<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 性能剖析

[English](profiling.md) | 简体中文

`foretoken bench --profile` 在一小段真实推理负载中采集 PyTorch CPU/GPU trace。命令会预热请求客户端，在所选模型的服务 Pod 上开启剖析，发送采集请求，最后停止采集并将文件取回本地。剖析会增加开销，因此这类结果用于诊断，不参与普通吞吐比较。

## 采集一次负载

集群需要使用支持 Torch profiler 的 vLLM runtime。操作账号须有权限查看 ModelService、ModelPool、ModelGroup 和 Pod，打开 Pod 端口转发，并从 model-server 容器复制文件。运行时镜像需要提供 `tar`，供 `kubectl cp` 使用。

通过维护的示例配置开启剖析能力，然后部署模型：

```bash
foretoken install --values examples/profiling/platform-values.yaml
foretoken deploy examples/quickstart
foretoken bench examples/quickstart \
  --profile --warmup-requests 8 --number 4 \
  --parallel 2 --prompt "解释天空为什么是蓝色的" --max-tokens 64
```

验证源码镜像时，沿用现有源码安装选项。开启剖析会改变 runtime 配置，需要先等待模型 Pod 更新就绪。只有执行 `--profile` 后才会实际开始采集。

`--warmup-requests` 是开启剖析前已派发的请求数量，响应不计入本次请求指标；仍在执行的预热请求可能出现在 trace 中。`--number` 是采集期间派发的请求数量。两个阶段复用同一个客户端、并发队列、生成参数和到达速率时钟。设置 `--warmup-requests 0` 可采集冷请求。

多模型部署使用 `--model` 选择模型。采集支持单个固定 prompt 或数据集来源，以及普通的 `--parallel`、`--rate` 和 `--open-loop` 负载选项；参数扫描、trace 回放、多数据集和 SLA 搜索属于其他评测模式。`--url` 不支持剖析，因为 URL 无法定位模型 Pod 和取回其产物。

## 查看结果

默认结果位于 `results/profiles/<session-id>/`，也可以通过 `--output-dir` 指定根目录。每个 Pod 的 trace 单独存放，`profile-run.json` 记录模型、请求配置、运行时镜像、预热数、采集指标和回收状态。剖析模式不会上传 W&B 或参与 Pareto 分析，不受普通 benchmark 输出默认值影响。

使用本地 Perfetto 或兼容 PyTorch 的查看器打开 `.pt.trace.json` 或 `.pt.trace.json.gz`，将 CPU 调度、CUDA kernel 和通信时间线与[服务指标](README_zh.md)对照。Nsight Systems 和 Nsight Compute 仍通过硬件平台单独使用，不是该命令的可选后端。

## 失败与清理

每个 model-server 同时只允许一个剖析会话，不改变正常推理准入。请求失败或 Ctrl+C 时，命令先结束自己仍在执行的请求，再停止已开启的采集并尝试取回文件。服务端根据负载推导出的截止时间为意外消失的客户端兜底，防止会话无限采集。

回收成功后，只删除该会话在 Pod 内的文件，并关闭端口转发。回收失败时，产物保留在 `/tmp/foretoken-profiles/<session-id>/`，错误信息会指出对应 Pod。先通过 Kubernetes 文件复制流程取回，再通过内部管理端点删除已停止的会话。Pod 重启会删除这些临时文件，采集期间不要更新或扩缩所选模型。

不再需要剖析时，将 runtime 的 profiling 选项关闭并重新执行 `foretoken install`。剖析端点只位于受限的 model-server API，不出现在公开推理前端。

参考：[vLLM profiling](https://docs.vllm.ai/en/latest/contributing/profiling/) 与 [PyTorch Profiler](https://docs.pytorch.org/tutorials/recipes/recipes/profiler_recipe.html)。
