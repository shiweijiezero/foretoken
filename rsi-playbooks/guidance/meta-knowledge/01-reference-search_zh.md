<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 01｜寻找参考资料

[English](01-reference-search.md) | 简体中文

寻找参考资料是为了了解现状、拓展思路。研究者根据研究目标和已有信息，自主选择需要参考的资料及获取方式：可以查阅本地源码和实验记录，也可以通过联网搜索了解其他方案与近期进展。对有启发的原理与实现继续深入研究，结合当前系统形成自己的研究方向，并将值得复用的发现整理到知识库。

## 常用来源与检索入口

### 本地知识、任务与实验

仓库内资料的路径相对 Foretoken 仓库根目录。实验产物在执行评测的机器上，根目录由 `--output-dir` 指定。

- 知识与元知识：[`rsi-playbooks/guidance/knowledge/`](../knowledge/README_zh.md) 和 `rsi-playbooks/guidance/meta-knowledge/`，分别查技术原理、实验经验与研究方法。
- 任务与模板：[`rsi-playbooks/tasks/`](../../tasks/README_zh.md)、[`rsi-playbooks/templates/`](../../templates/README_zh.md)，查目标负载、资源约束、历史交付要求及知识记录格式。
- 实验总结：`<output-dir>/notes/experiment.md`，查整体发现、保留方案和后续问题；`<output-dir>/iterations/<iteration>/notes/iteration.md` 查各轮假设、改动、结果与取舍。
- 原始证据：`<output-dir>/iterations/<iteration>/runs/<command>-NNNNNN/generated/context.json`、`generated/changes/` 和 `artifacts/<结果目录>/`，查命令、源码改动、配置、环境、指标、响应与日志。后两个路径均相对同一运行目录。完整布局见 [`rsi-playbooks/experiment-records_zh.md`](../../experiment-records_zh.md)。
- 性能与负载：[`benchmarks/docs/recipes_zh.md`](../../../benchmarks/docs/recipes_zh.md)、[`benchmarks/docs/perf/README_zh.md`](../../../benchmarks/docs/perf/README_zh.md)、[`benchmarks/docs/metrics_zh.md`](../../../benchmarks/docs/metrics_zh.md)，查可运行负载、参数扫描、真实轨迹、SLO 与指标定义。
- 质量与剖析：[`benchmarks/docs/eval/README_zh.md`](../../../benchmarks/docs/eval/README_zh.md)、[`benchmarks/docs/eval/distribution-comparison_zh.md`](../../../benchmarks/docs/eval/distribution-comparison_zh.md)、[`benchmarks/docs/profile/README_zh.md`](../../../benchmarks/docs/profile/README_zh.md)，查逐题报告、概率分布比较及执行时间线。
- 观测：[`observability/README_zh.md`](../../../observability/README_zh.md)、[`observability/runbooks/alerts_zh.md`](../../../observability/runbooks/alerts_zh.md)，查指标、日志和排障方法。实际记录规则位于 [`deploy/charts/foretoken/files/recording-rules.yaml`](../../../deploy/charts/foretoken/files/recording-rules.yaml)，看板位于同目录的 [`grafana/`](../../../deploy/charts/foretoken/files/grafana/)。
- 配置与运行示例：[`docs/custom-deployment_zh.md`](../../../docs/custom-deployment_zh.md)、[`docs/inference-parameters_zh.md`](../../../docs/inference-parameters_zh.md)、[`examples/recipes/`](../../../examples/recipes/)、[`examples/feature/encoder-prefill-decode/`](../../../examples/feature/encoder-prefill-decode/)、[`examples/feature/shared-kv-store/`](../../../examples/feature/shared-kv-store/)，查源码部署、引擎参数、分离服务和缓存配置。
- 引擎源码与补丁：`data-plane/third_party/vllm/`、`third_party/mooncake/` 是子模块源码入口；[`.gitmodules`](../../../.gitmodules) 提供仓库地址，Git gitlink 记录固定提交。[`data-plane/patches/vllm/`](../../../data-plane/patches/vllm/) 查 Foretoken 补丁；`deploy/inference-engines/*/source-environment.json` 查运行环境和硬件适配来源。
- 项目历史：[Foretoken Issues](https://github.com/shiweijiezero/foretoken/issues)、[PR](https://github.com/shiweijiezero/foretoken/pulls) 及本地 Git 历史，查过去的讨论、修复、回退和最终实现。

### Issue、PR 与源码的查找方式

- Issue：同时查开放和已关闭的问题，阅读复现条件、日志、讨论及最后结论。按模型名、算子名、错误文本、负载特征和性能指标组合检索。
- PR：沿 Issue 的关联 PR 阅读实现差异、评审、测量和合并状态；合并后对照目标版本的实际代码。已关闭但未合并的方案也可能包含有用的失败原因和设计取舍。
- 设计讨论：查项目的 RFC、proposal、开发论坛、公开会议记录和演讲，寻找尚未进入实现的方案，以及设计者考虑过的替代路径。
- 源码与历史：查实现、测试、benchmark、examples、release notes，再用提交历史定位机制引入、修改或回退的原因。

GitHub 检索示例：

```text
repo:vllm-project/vllm is:issue "prefill" "latency"
repo:vllm-project/vllm is:pr is:merged "sampling"
repo:sgl-project/sglang "all-reduce" in:comments
repo:vllm-project/vllm path:vllm "max_num_batched_tokens"
```

前三行用于 Issue／PR 搜索，最后一行用于代码搜索。语法见 [Issue／PR 检索](https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests)、[代码检索](https://docs.github.com/en/search-github/github-code-search/understanding-github-code-search-syntax)和[讨论检索](https://docs.github.com/en/search-github/searching-on-github/searching-discussions)。

本地检索示例：

```bash
# 在知识、文档与源码中寻找机制和调用位置
rg -n -i 'prefill|expert|sampling' rsi-playbooks benchmarks data-plane docs

# 查某个机制的变更与实际差异
git log -G 'spec.decode|chunked.prefill' -- data-plane deploy
git show <commit> -- <path>

# 查看上游问题讨论及关联实现
gh issue view <number> --repo vllm-project/vllm --comments
gh pr view <number> --repo vllm-project/vllm --comments
gh pr diff <number> --repo vllm-project/vllm
```

将 `<commit>`、`<path>` 和 `<number>` 替换为检索得到的提交、路径与条目编号。检索上游源码或实验结果时，显式指定相应目录。

### 推理引擎与模型执行

- vLLM：[Issues](https://github.com/vllm-project/vllm/issues)、[PR](https://github.com/vllm-project/vllm/pulls)、[官方论坛](https://discuss.vllm.ai/)、[文档](https://docs.vllm.ai/)、[博客](https://vllm-project.github.io/)。查调度、预填充分块、KV、模型适配、性能回归和 RFC；社区问答主要进入官方论坛。
- SGLang：[Issues](https://github.com/sgl-project/sglang/issues)、[PR](https://github.com/sgl-project/sglang/pulls)、[Discussions](https://github.com/sgl-project/sglang/discussions)、[文档](https://docs.sglang.io/)、[Cookbook](https://cookbook.sglang.io/)。查 Radix 缓存、MoE、推测解码、分离部署及优化实现；仓库 `benchmark/`、`examples/` 提供测量与复现线索。
- TensorRT-LLM：[Issues](https://github.com/NVIDIA/TensorRT-LLM/issues)、[Discussions](https://github.com/NVIDIA/TensorRT-LLM/discussions)、[文档](https://nvidia.github.io/TensorRT-LLM/)、[性能指南](https://nvidia.github.io/TensorRT-LLM/developer-guide/perf-overview.html)。查 NVIDIA 后端、attention、量化、批处理与性能取舍。
- Transformers：[Issues](https://github.com/huggingface/transformers/issues)、[PR](https://github.com/huggingface/transformers/pulls)、[文档](https://huggingface.co/docs/transformers/index)。查模型结构、配置、attention mask、RoPE、tokenizer、权重加载及模型支持的引入过程。
- MetaX vLLM：[Issues](https://github.com/MetaX-MACA/vLLM-metax/issues)、[源码与示例](https://github.com/MetaX-MACA/vLLM-metax)、[文档](https://vllm-metax.readthedocs.io/en/latest/)。查 MACA 运行条件、插件适配和后端缺口；工程交流可从 [vLLM Slack](https://slack.vllm.ai) 进入 `#sig-maca`。

### 分布式服务、路由与控制面

- Dynamo：[Issues](https://github.com/ai-dynamo/dynamo/issues)、[Discussions](https://github.com/ai-dynamo/dynamo/discussions)、[文档](https://docs.nvidia.com/dynamo/latest)、[recipes](https://github.com/ai-dynamo/dynamo/tree/main/recipes)。查 KV 感知路由、planner、P/D 分离、分层缓存与故障处理。
- llm-d：[Issues](https://github.com/llm-d/llm-d/issues)、[proposals](https://github.com/llm-d/llm-d/tree/main/proposals)、[架构](https://llm-d.ai/docs/architecture)、[部署路径](https://llm-d.ai/docs/well-lit-paths)。查调度、缓存、wide EP 和 Kubernetes 推理组织方式。
- llm-d 社区：[贡献者邮件组](https://groups.google.com/g/llm-d-contributors)、[公开会议日历](https://red.ht/llm-d-public-calendar)、[Slack](https://llm-d.ai/slack)。查正在讨论的设计、替代方案和开发计划。
- AIBrix：[Issues](https://github.com/vllm-project/aibrix/issues)、[PR](https://github.com/vllm-project/aibrix/pulls)、[Router 设计](https://aibrix.readthedocs.io/latest/designs/aibrix-router.html)、[benchmarks](https://github.com/vllm-project/aibrix/tree/main/benchmarks)。查负载与前缀感知路由、LoRA、扩缩容和请求优先级。
- Gateway API Inference Extension：[Issues](https://github.com/kubernetes-sigs/gateway-api-inference-extension/issues)、[设计提案](https://github.com/kubernetes-sigs/gateway-api-inference-extension/tree/main/docs/proposals)。查 InferencePool、EPP 和 filter／scorer／picker 的接口及调度观测设计。
- KServe：[Issues](https://github.com/kserve/kserve/issues)、[社区与会议入口](https://github.com/kserve/community)、[设计文档索引](https://github.com/kserve/community/blob/main/design_docs.md)。查服务 API、runtime 生命周期、推理图和部署模式。
- vLLM Production Stack：[Issues](https://github.com/vllm-project/production-stack/issues)、[文档](https://docs.vllm.ai/projects/production-stack)、[教程](https://github.com/vllm-project/production-stack/tree/main/tutorials)。查引擎、路由、缓存和 Helm 的整体集成，以及多轮请求实验。
- Ray Serve：[Issues](https://github.com/ray-project/ray/issues)、[LLM 架构](https://docs.ray.io/en/latest/serve/llm/architecture/index.html)、[服务 benchmark](https://docs.ray.io/en/latest/serve/llm/benchmarks.html)、[论坛](https://discuss.ray.io/)。查多模型、副本、placement、扩缩容和分布式恢复。

### KV 缓存、存储与数据传输

- LMCache：[Issues](https://github.com/LMCache/LMCache/issues)、[Discussions](https://github.com/LMCache/LMCache/discussions)、[文档](https://docs.lmcache.ai/)、[社区会议](https://docs.lmcache.ai/community/meetings.html)、[benchmark 指南](https://docs.lmcache.ai/getting_started/benchmarking.html)。查 KV 卸载、分层存储、P2P、connector 和实际缓存收益。
- Mooncake：[Issues](https://github.com/kvcache-ai/Mooncake/issues)、[文档](https://kvcache-ai.github.io/Mooncake/)、[FAST25 复现与 trace](https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release)。查分布式 KV、Transfer Engine、RDMA／TCP 和真实请求轨迹。
- NIXL：[Issues](https://github.com/ai-dynamo/nixl/issues)、[Backend Guide](https://github.com/ai-dynamo/nixl/blob/main/docs/BackendGuide.md)、[NIXLBench](https://github.com/ai-dynamo/nixl/blob/main/benchmark/nixlbench/README.md)。查内存注册、跨内存层传输、异步完成和消息大小相关开销。
- 3FS：[Issues](https://github.com/deepseek-ai/3FS/issues)、[设计与操作资料](https://github.com/deepseek-ai/3FS/tree/main/docs)、[benchmarks](https://github.com/deepseek-ai/3FS/tree/main/benchmarks)。查分离式存储、权重读取、随机 I/O、聚合吞吐与尾延迟。
- Seastar：[源码与 Issues](https://github.com/scylladb/seastar)、[教程](https://github.com/scylladb/seastar/blob/master/doc/tutorial.md)、[开发邮件组](https://groups.google.com/g/seastar-dev)。查 reactor、分核执行、背压和 I/O 调度，为前端与缓存服务设计寻找跨领域思路。

### 通信、网络与跨节点协作

- DeepEP：[源码、Issues 与 PR](https://github.com/deepseek-ai/DeepEP)。查 MoE dispatch／combine、低延迟通信、SM 分配、拓扑和计算通信重叠。
- NCCL：[Issues](https://github.com/NVIDIA/nccl/issues)、[用户指南](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/index.html)、[nccl-tests](https://github.com/NVIDIA/nccl-tests)。查 collective 算法、拓扑、网络插件、hang 与多节点带宽。
- RCCL：[文档](https://rocm.docs.amd.com/projects/rccl/en/latest/)、[当前源码与测试所在的 rocm-systems](https://github.com/ROCm/rocm-systems)。查 AMD 集合通信、拓扑和性能；历史问题可继续检索旧 `ROCm/rccl`、`ROCm/rccl-tests` 仓库。
- UCX：[Issues](https://github.com/openucx/ucx/issues)、[文档](https://openucx.readthedocs.io/en/master/)、[开发邮件组](https://elist.ornl.gov/mailman/listinfo/ucx-group)。查 transport 选择、内存注册、RDMA／RoCE、共享内存与 GPU 传输；源码中的 `src/tools/perf/ucx_perftest` 提供测量线索。
- UCCL：[源码与 Issues](https://github.com/uccl-project/uccl)、[项目资料](https://uccl-project.github.io/)、[P2P 说明](https://github.com/uccl-project/uccl/blob/main/p2p/README.md)。查 CPU 驱动网络、拥塞控制、collective／P2P／EP 和不同 NIC 的执行路径。
- Envoy：[Issues](https://github.com/envoyproxy/envoy/issues)、[文档](https://www.envoyproxy.io/docs/envoy/latest/)、[Overload Manager](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/operations/overload_manager.html)、[性能框架](https://github.com/envoyproxy/envoy-perf)。查连接池、流式响应、取消、超时、背压和过载调度；仓库 README 提供会议与邮件组入口。

### 其他执行架构与跨平台后端

- llama.cpp：[Issues](https://github.com/ggml-org/llama.cpp/issues)、[Discussions](https://github.com/ggml-org/llama.cpp/discussions)、[生成性能指南](https://github.com/ggml-org/llama.cpp/blob/master/docs/development/token_generation_performance_tips.md)。查 GGUF、CPU／GPU 卸载、量化、Metal／Vulkan／HIP／SYCL 等后端，以及低内存条件下的执行取舍。
- LMDeploy：[源码与 Issues](https://github.com/InternLM/lmdeploy)、[文档](https://lmdeploy.readthedocs.io/en/latest/)。查 TurboMind 与 PyTorch 两类引擎、量化、KV 管理和多模态执行；仓库 `benchmark/` 提供测量实现。
- FastDeploy：[Issues](https://github.com/PaddlePaddle/FastDeploy/issues)、[文档](https://paddlepaddle.github.io/FastDeploy/)、[benchmarks](https://github.com/PaddlePaddle/FastDeploy/tree/develop/benchmarks)。查 Paddle 生态的 LLM／VLM、推测解码、P/D 分离及多种国产加速器适配。
- MLC LLM：[源码与 Issues](https://github.com/mlc-ai/mlc-llm)、[文档](https://llm.mlc.ai/docs/)。查基于编译的模型部署、量化、模型库以及 CUDA、ROCm、Vulkan、WebGPU 等跨平台执行。
- OpenVINO GenAI：[源码与 Issues](https://github.com/openvinotoolkit/openvino.genai)、[文档](https://openvinotoolkit.github.io/openvino.genai/)。查 Intel CPU／GPU／NPU、连续批处理、推测解码、KV eviction 和多模态 pipeline。

### 算子、编译器与执行运行时

- PyTorch：[Issues](https://github.com/pytorch/pytorch/issues)、[论坛](https://discuss.pytorch.org/)、[编译性能分析](https://docs.pytorch.org/docs/stable/user_guide/torch_compiler/torch.compiler_profiling_torch_compile.html)。查 graph break、重编译、Inductor、算子选择、Python／launch 开销和 CUDA Graph。
- Triton：[Issues](https://github.com/triton-lang/triton/issues)、[教程](https://triton-lang.org/main/getting-started/tutorials/)、[调试指南](https://triton-lang.org/main/programming-guide/chapter-3/debugging.html)。查布局、融合、生成代码、后端差异、autotuning 与编译器优化。
- LLVM／MLIR：[源码与 Issues](https://github.com/llvm/llvm-project)、[GPU dialect](https://mlir.llvm.org/docs/Dialects/GPU/)、[Passes](https://mlir.llvm.org/docs/Passes/)、[论坛](https://discourse.llvm.org/)。查 lowering、内存变换、同步和目标指令生成。
- TVM：[源码与 Issues](https://github.com/apache/tvm)、[MetaSchedule](https://tvm.apache.org/docs/deep_dive/tensor_ir/tutorials/meta_schedule.html)、[论坛](https://discuss.tvm.apache.org/)。查调度搜索、目标设备测量、候选选择和编译部署。
- CUTLASS／CuTe：[Issues](https://github.com/NVIDIA/cutlass/issues)、[Discussions](https://github.com/NVIDIA/cutlass/discussions)、[文档](https://docs.nvidia.com/cutlass/)。查 GEMM、tile、流水线、epilogue 和架构专用实现。
- FlashInfer：[源码与 Issues](https://github.com/flashinfer-ai/flashinfer)、[文档](https://docs.flashinfer.ai/)、[社区讨论](https://github.com/orgs/flashinfer-ai/discussions)、[benchmarks](https://github.com/flashinfer-ai/flashinfer/tree/main/benchmarks)。查 attention、KV layout、sampling、dispatch 与 graph capture。
- AMD 算子库：[rocm-libraries](https://github.com/ROCm/rocm-libraries)、[hipBLASLt](https://rocm.docs.amd.com/projects/hipBLASLt/en/latest/)、[rocBLAS](https://rocm.docs.amd.com/projects/rocBLAS/en/latest/)、[AITER](https://github.com/ROCm/aiter)。查 GEMM 算法选择、Composable Kernel、特定 shape 调优和推理算子。CK、hipBLASLt 的当前开发入口集中在 rocm-libraries。
- MetaX 算子与编程：[maca-samples](https://github.com/MetaX-MACA/maca-samples)、[mcoplib](https://github.com/MetaX-MACA/mcoplib)、[mcTriton](https://github.com/MetaX-MACA/mcTriton)、[op_optimization](https://github.com/MetaX-MACA/op_optimization)。查 MACA 编程示例、算子实现、Triton 后端和设备适配。

### 硬件架构、编程指南与厂商社区

- NVIDIA：[CUDA Programming Guide](https://docs.nvidia.com/cuda/cuda-programming-guide/index.html)、[Best Practices](https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/index.html)、[Hopper](https://docs.nvidia.com/cuda/hopper-tuning-guide/index.html) 与 [Blackwell 调优指南](https://docs.nvidia.com/cuda/blackwell-tuning-guide/index.html)。查内存层次、线程调度、Tensor Core、异步复制和设备特定约束。
- AMD：[GPU 架构索引](https://rocm.docs.amd.com/en/latest/reference/gpu-arch/index.html)、[HIP 性能指南](https://rocm.docs.amd.com/projects/HIP/en/latest/how-to/performance_guidelines.html)、[工作负载优化](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/optimization/workload-optimization.html)。查 CDNA、MFMA、寄存器、LDS、HBM 和多 chiplet 行为。
- MetaX：[开发者文档中心](https://developer.metax-tech.com/doc)、[官方源码组织](https://github.com/MetaX-MACA)。按芯片和 SDK 版本查编程、通信、工具与运行环境；[mccl_tests](https://github.com/MetaX-MACA/mccl_tests) 和 [mccl-nccl](https://github.com/MetaX-MACA/mccl-nccl) 提供通信实现与测试入口。
- CPU 架构：[Intel Optimization Reference Manual](https://www.intel.com/content/www/us/en/developer/articles/technical/intel64-and-ia32-architectures-optimization.html)、[AMD Processor Resources](https://www.amd.com/en/developer/browse-by-product-type/processor-resources.html)。查缓存、向量指令、内存访问、处理器调优和硬件计数器定义。
- 厂商问题讨论：[NVIDIA Developer Forums](https://forums.developer.nvidia.com/)、[ROCm Developer Hub](https://www.amd.com/en/developer/resources/rocm-hub.html)、[ROCm 系统讨论](https://github.com/ROCm/rocm-systems/discussions)。查驱动、工具、设备行为、已知问题与维护者答复。
- 网络与设备健康：[NVIDIA Networking 文档](https://docs.nvidia.com/networking/)、[DCGM Diagnostics](https://docs.nvidia.com/datacenter/dcgm/latest/learn/modules/dcgm-diagnostics.html)、[Linux RDMA](https://github.com/linux-rdma/rdma-core)、[perftest](https://github.com/linux-rdma/perftest)。查 NIC、PCIe／NVLink、NUMA、RoCE、设备错误和底层带宽／延迟。
- GPU 发起通信：[NVSHMEM 文档](https://docs.nvidia.com/nvshmem/index.html)、[源码](https://github.com/NVIDIA/nvshmem)、[GDRCopy](https://github.com/NVIDIA/gdrcopy)。查单边通信、kernel 内协调和 CPU 访问 GPU 映射内存的成本。

### 性能剖析与系统分析方法

- NVIDIA：[Nsight Systems](https://docs.nvidia.com/nsight-systems/UserGuide/index.html) 查 CPU／GPU 时间线、launch gap、同步与通信重叠；[Nsight Compute](https://docs.nvidia.com/nsight-compute/ProfilingGuide/) 查 kernel counters、访存、occupancy 和 roofline。
- PyTorch：[Profiler 教程](https://docs.pytorch.org/tutorials/beginner/profiler.html)、[内存快照](https://docs.pytorch.org/docs/stable/torch_cuda_memory)。查算子、CPU 栈、设备执行与分配器行为的对应关系。
- AMD：[Profiling Tools Overview](https://rocm.docs.amd.com/en/latest/components/profiling-tools-overview.html)、[rocprofv3](https://rocm.docs.amd.com/projects/rocprofiler-sdk/en/latest/how-to/using-rocprofv3.html)、[Compute Profiler](https://rocm.docs.amd.com/projects/rocprofiler-compute/en/latest/)、[Systems Profiler](https://rocm.docs.amd.com/projects/rocprofiler-systems/en/latest/)。当前源码集中在 [rocm-systems](https://github.com/ROCm/rocm-systems)。
- MetaX：[性能工具导航](https://developer.metax-tech.com/doc?category_ids=61)。选择匹配 SDK 的 mcTracer、mcProfiler 手册，查运行采集、attach、MCTX 标记、时间线和硬件指标。
- Perfetto：[SQL 分析入门](https://perfetto.dev/docs/analysis/perfetto-sql-getting-started)、[Trace Processor CLI](https://perfetto.dev/docs/reference/trace-processor-cli)。查如何从原始 trace 提取阶段耗时、重叠和等待，形成可重复的分析。
- Linux：[Perf Wiki](https://perfwiki.github.io/main/tutorial/)、[cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)、[NUMA memory policy](https://docs.kernel.org/admin-guide/mm/numa_memory_policy.html)。查 CPU 采样、调度、限流、内存压力和 NUMA 放置。
- CPU profiler：[Intel VTune](https://www.intel.com/content/www/us/en/docs/vtune-profiler/user-guide/2026-0/analysis-types.html)、[AMD uProf](https://docs.amd.com/r/en-US/57368-uProf-user-guide)。查主机流水线、缓存缺失、内存带宽和远端 NUMA 访问。
- 性能分析方法：[NERSC Roofline](https://docs.nersc.gov/tools/performance/roofline/)、[Empirical Roofline Toolkit](https://bitbucket.org/berkeleylab/cs-roofline-toolkit/)、[Brendan Gregg USE Method](https://www.brendangregg.com/usemethod.html)。查实测计算／带宽上限，以及资源利用率、饱和度和错误的系统化分析。

### 论文检索、引用脉络与正式出版

- [arXiv](https://arxiv.org/search/advanced)：按标题、摘要、作者和时间发现新工作，查看版本历史、正式发表信息与代码链接。推理系统可交叉检索 `cs.DC`、`cs.PF`、`cs.AR`、`cs.DB`、`cs.LG` 等类别。
- [OpenReview](https://openreview.net/)：查论文、公开评审、作者回复与修订，理解基线选择、消融、适用条件和争议。
- [DBLP](https://dblp.org/)：核对准确标题、作者、会议和正式版本，沿作者与会议索引追踪连续工作。
- [Semantic Scholar](https://www.semanticscholar.org/)：沿参考文献、被引论文和相关推荐，查前提来源、后续改进与独立重评估。
- 作者与实验室主页：从论文作者进入其 publications、项目和博客，例如 [Tri Dao](https://tridao.me/)、[Hao AI Lab](https://haoailab.com/)。查代码、slides、后续研究及尚未写进论文的解释。
- 综述与重评估：例如 [A Survey of LLM Inference Systems](https://arxiv.org/abs/2506.21901) 帮助建立机制分类；[分离推理性能与能耗重评估](https://arxiv.org/abs/2601.08833)帮助发现工作负载与传输介质带来的边界。沿引用继续寻找新进展与相反结果。

### 会议论文、演讲与复现材料

- ML 系统与模型算法：[MLSys](https://proceedings.mlsys.org/)、[NeurIPS](https://proceedings.neurips.cc/)、[ICML／PMLR](https://proceedings.mlr.press/)、[ICLR／OpenReview](https://openreview.net/)。查 serving、compiler、runtime、模型结构、推测解码、稀疏化与压缩。
- 系统与网络：[OSDI](https://www.usenix.org/conference/osdi25/technical-sessions)、[NSDI](https://www.usenix.org/conference/nsdi26/technical-sessions)、[SOSP](https://sigops.org/s/conferences/sosp/2025/accepted.html)、[SIGCOMM](https://conferences.sigcomm.org/sigcomm/2025/program/papers-info/)。查分离架构、KV、调度、资源隔离、RDMA 和多节点服务；从各年度页面进入论文、演讲、slides 与 artifact。
- 体系结构：[ASPLOS](https://www.asplos-conference.org/asplos2026/program/)、[ISCA](https://www.iscaconf.org/isca2026/program/)、[MICRO](https://microarch.org/micro58/program/index.php)、[HPCA](https://2026.hpca-conf.org/program/program-hpca-2026/)。查软硬件协同、内存层级、互连、加速器和数据流。
- 并行与 HPC：[PPoPP](https://ppopp25.sigplan.org/)、[SC](https://sc25.supercomputing.org/program/papers/)。查同步、并行 runtime、编译、拓扑和规模化通信。
- Artifact 与实验附件：[OSDI Artifact](https://www.usenix.org/conference/osdi26/call-for-artifacts)、[SC 复现入口](https://sc25.supercomputing.org/program/papers/reproducibility-initiative/)、[PPoPP Artifact](https://ppopp25.sigplan.org/track/PPoPP-2025-artifact-evaluation)。沿论文查固定版本、环境、脚本、原始结果、评审记录和复现说明。

### 技术博客、模型团队与公开课程

- 框架工程博客：[LMSYS](https://lmsys.org/blog/)、[PyTorch](https://pytorch.org/blog/accelerating-moe-model/)、[LMCache](https://blog.lmcache.ai/)、[FlashInfer](https://flashinfer.ai/)。查具体执行机制、实现过程、性能证据和后续问题，再跟踪关联代码。
- 厂商技术资料：[NVIDIA Technical Blog](https://developer.nvidia.com/blog/)、[ROCm Blog](https://rocm.blogs.amd.com/)。查新硬件、内核、通信、图执行和 profiler 的工程案例。
- 研究团队与云端实践：[Google Research](https://research.google/pubs/)、[Microsoft Research 的 Splitwise](https://www.microsoft.com/en-us/research/blog/splitwise-improves-gpu-usage-by-splitting-llm-inference-phases/)、[Together AI 的 Flash-Decoding](https://www.together.ai/blog/flash-decoding-for-long-context-inference)、[Hao AI Lab 的 DistServe 回顾](https://haoailab.com/blogs/distserve-retro/)。查阶段划分、异构资源、算法与系统协同，以及从研究走向服务后的设计变化。
- 模型团队系统资料：[DeepSeek Open Infra](https://github.com/deepseek-ai/open-infra-index)、[DeepSeek-V3](https://github.com/deepseek-ai/DeepSeek-V3)。查模型技术报告、推理架构、DeepEP、DeepGEMM、FlashMLA、专家负载均衡及 profile 数据。
- 架构研究社区：[SIGARCH](https://www.sigarch.org/tag/machine-learning-systems/)。查跨领域问题、研究观点、workshop 和软硬件协同线索。
- 公开讲座：[Stanford MLSys Seminar](https://mlsys.stanford.edu/)、[GPU MODE 讲次材料](https://github.com/gpu-mode/lectures)、[GPU MODE 视频](https://www.youtube.com/@GPUMODE)。查 profiling、Triton、量化、通信、多 GPU、编译与数值行为，并结合对应代码和课件阅读。
- 开发者会议：[Triton Developer Conference 材料](https://github.com/triton-lang/triton/blob/main/docs/meetups/dev_conference_2025.md)。从议程进入演讲、slides 和实现，理解后端、编译器与 kernel 的设计取舍。

### 设计空间、模拟与结构创新实现

- [FastAFD](https://github.com/hao-ai-lab/FastAFD)：查 Attention／FFN 分离、通信重叠、对齐验证和实验脚本，分析其硬件与执行条件。
- [Vidur](https://github.com/microsoft/vidur)：查以 profiling 为基础的推理模拟、请求 trace 和配置探索方法。
- [AFD-Ledger](https://github.com/kvcache-ai/AFD-Ledger)：查硬件配比、显存、TPOT 与预算约束下的分离部署分析。

查结构性方案时，可组合 `attention FFN disaggregation`、`disaggregated expert parallelism`、`operator-level disaggregation` 等同义词；进一步用 `dataflow`、`locality`、`pipeline scheduling`、`communication avoidance`、`heterogeneous scheduling` 和 `memory disaggregation` 搜索并行计算、数据库、网络与架构中的相关机制。

### 模型、量化、推测解码与注意力实现

- Hugging Face 模型页与文件：例如 [Qwen3-Next 模型卡](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct)、[config.json](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct/blob/main/config.json)、[DeepSeek-V3 模型卡](https://huggingface.co/deepseek-ai/DeepSeek-V3)、[config.json](https://huggingface.co/deepseek-ai/DeepSeek-V3/blob/main/config.json)。查模型结构、混合注意力、MoE、MLA、MTP、量化、RoPE、tokenizer 和聊天模板；结合目标模型的实际配置理解执行需求。
- 模型社区：[Hugging Face Forums](https://discuss.huggingface.co/)、模型页的 Community，例如 [Qwen3-Next Discussions](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct/discussions)。查模型与运行时兼容、显存、推测模型配置及异常输出的复现与答复。
- 量化：[llm-compressor](https://github.com/vllm-project/llm-compressor)、[NVIDIA Model Optimizer](https://github.com/NVIDIA/Model-Optimizer)。查校准、低比特权重与 KV、部署格式、精度和实际 kernel 的关系。
- 推测解码：[Dynamic Speculation](https://huggingface.co/blog/dynamic_speculation_lookahead)、[SPEED-Bench 数据](https://huggingface.co/datasets/nvidia/SPEED-Bench)、[实现说明](https://github.com/NVIDIA-NeMo/Gym/blob/main/benchmarks/speed-bench/README.md)。查自适应 lookahead、接受率、草稿成本和任务分布对收益的影响。
- 注意力与线性注意力：[FlashAttention](https://github.com/Dao-AILab/flash-attention)、[Flash Linear Attention](https://github.com/fla-org/flash-linear-attention)。查 attention kernel、KV cache、线性注意力、状态更新、混合模型及其推理实现。
- 算子设计语言：[TileLang](https://github.com/tile-ai/tilelang)、[文档](https://tilelang.com/)。查 GEMM、量化、attention、布局、流水线和 autotuning 的编写方式。
- 昇腾后端：[vLLM Ascend Issues](https://github.com/vllm-project/vllm-ascend/issues)、[文档](https://docs.vllm.ai/projects/ascend/en/latest/)、[支持矩阵](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/support_matrix/)。查 Ascend 模型执行、并行和算子适配的具体条件。

### 真实工作负载与长上下文数据

- [Azure Public Dataset](https://github.com/Azure/AzurePublicDataset)、[LLM Inference Dataset 2024](https://github.com/Azure/AzurePublicDataset/blob/master/AzureLLMInferenceDataset2024.md)：查请求时间与输入输出长度分布。该 trace 提供时间戳和 token 数，适合到达过程与长度研究；文本内容另选相应数据集。
- [BurstGPT](https://github.com/hpmll/BurstGPT)：查突发、会话关联、请求失败和过滤方式，以及它们怎样影响服务负载。
- [LMSYS-Chat-1M](https://huggingface.co/datasets/lmsys/lmsys-chat-1m)：查真实多轮内容、任务与语言分布；到达过程可结合有时间戳的 trace 单独研究。
- [Mooncake FAST25 数据与实验](https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release)：查输入输出长度、共享前缀和缓存行为相关的真实负载。
- [RULER](https://github.com/NVIDIA/RULER)、[LongBench](https://github.com/THUDM/LongBench)：查有效上下文长度、检索、多跳、聚合、长文档与代码任务，以及生成和评分脚本。
- [MMLongBench](https://github.com/EdinburghNLP/MMLongBench)：查 Visual RAG、PDF 摘要、文档问答和长多模态上下文，理解视觉编码、图片 token 与文本共同形成的开销。

### 公开性能报告、评测器与复现脚本

- MLPerf：[参考实现](https://github.com/mlcommons/inference)、[公开提交示例](https://github.com/mlcommons/inference_results_v5.1)、[AMD 复现说明](https://rocm.blogs.amd.com/artificial-intelligence/mlperf-inference5.1-repro/README.html)。查标准负载、LoadGen、软硬件配置、代码和测量结果；按所研究版本选择对应提交。
- [GuideLLM](https://github.com/vllm-project/guidellm)、[AIPerf](https://github.com/ai-dynamo/aiperf)：查并发、请求速率、多轮、真实 trace、多模态负载和客户端计时实现，以及机器可读结果与 telemetry。
- [InferenceX](https://github.com/SemiAnalysisAI/InferenceX)：沿公开成绩查硬件、引擎、collective／operator 配置和执行材料；原项目名为 InferenceMAX。
- [独立引擎复现](https://github.com/Michaelvll/llm-ie-benchmarks)：查 vLLM／SGLang 的部署配置、benchmark 与原始日志，分析样本量和负载如何改变比较结果。
- [Artificial Analysis 测量方法](https://artificialanalysis.ai/methodology)：查用户侧服务的 TTFT、输出速度、reasoning、缓存与 tokenization 口径。
- 性能争议与测量修复：例如 [vLLM 推测加速未复现](https://github.com/vllm-project/vllm/issues/10318)、[量化草稿变慢](https://github.com/vllm-project/vllm/issues/21278)、[SGLang 长上下文比较](https://github.com/sgl-project/sglang/issues/3471)。查实际环境、命令、失败条件和后续结论。
- 评测输入管线：[vLLM PR #25916](https://github.com/vllm-project/vllm/pull/25916) 修正重复 BOS 对 EAGLE3 接受率的影响。此类 PR 提供聊天模板、分词和客户端行为如何改变测量结果的直接线索。

### 客户端、前端与模型加载

- Rust 异步执行：[Tokio 同步与异步边界](https://tokio.rs/tokio/topics/bridging)、[tracing](https://tokio.rs/tokio/topics/tracing)。查运行时阻塞、并发任务与 span 关联，解释 frontend／router 等待。
- Python：[asyncio 开发指南](https://docs.python.org/3/library/asyncio-dev.html)。查慢回调、事件循环停顿、线程交互与调试方式。
- 流式协议与代理：[SSE 规范](https://html.spec.whatwg.org/multipage/server-sent-events.html)、[Envoy flow control](https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/flow_control)。查事件边界、buffering 和 HTTP/2 流控，解释引擎产出与客户端接收之间的时间差。
- CPU 推理：[vLLM CPU 文档](https://docs.vllm.ai/en/stable/getting_started/installation/cpu/)。查 OpenMP、物理核、NUMA、rank 放置和服务线程竞争。
- 下载与存储：[Hugging Face 模型下载](https://huggingface.co/docs/hub/models-downloading)、[fio](https://github.com/axboe/fio)。查 Xet、并发下载、缓存、懒加载及独立 I/O 测量，分开研究下载、权重加载与推理阶段。
- 更广泛的公开讨论：[Hacker News Search](https://hn.algolia.com/)可发现项目发布、工程复盘和作者讨论，再沿链接进入论文、源码及可复现材料。
