<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 01 | Finding Reference Material

English | [简体中文](01-reference-search_zh.md)

Reference material helps explain the current state of the field and broaden the ideas available for research. Choose sources and how to obtain them according to the research objective and evidence already available: local code and experiment records may be useful, as may online searches for other approaches and recent developments. Study promising principles and implementations in depth, develop a research direction in the context of the current system, and add reusable findings to the knowledge base.

## Sources and search entry points

### Local knowledge, tasks, and experiments

Repository paths below are relative to the Foretoken repository root. Experiment artifacts reside on the machine that ran the evaluation, under the root selected with `--output-dir`.

- Knowledge and meta-knowledge: [`rsi-playbooks/guidance/knowledge/`](../knowledge/README.md) and `rsi-playbooks/guidance/meta-knowledge/` cover technical principles and experimental experience, and research methods, respectively.
- Tasks and templates: [`rsi-playbooks/tasks/`](../../tasks/README.md) and [`rsi-playbooks/templates/`](../../templates/README.md) provide target workloads, resource constraints, previous delivery requirements, and formats for knowledge notes.
- Experiment summaries: the task's `report.md` records overall findings, retained approaches, remaining questions and links to iterations; `<output-dir>/iterations/<iteration>/notes/iteration.md` records each iteration's hypothesis, changes, results, and trade-offs.
- Raw evidence: `<output-dir>/iterations/<iteration>/runs/<command>-NNNNNN/generated/context.json`, `generated/changes/`, and `artifacts/<result-directory>/` contain commands, source changes, configuration, environment details, metrics, responses, and logs. The latter two paths are relative to the same run directory. See [`rsi-playbooks/experiment-records.md`](../../experiment-records.md) for the full layout.
- Performance and workloads: [`benchmarks/docs/recipes.md`](../../../benchmarks/docs/recipes.md), [`benchmarks/docs/perf/README.md`](../../../benchmarks/docs/perf/README.md), and [`benchmarks/docs/metrics.md`](../../../benchmarks/docs/metrics.md) cover runnable workloads, parameter sweeps, real traces, SLOs, and metric definitions.
- Quality and profiling: [`benchmarks/docs/eval/README.md`](../../../benchmarks/docs/eval/README.md), [`benchmarks/docs/eval/distribution-comparison.md`](../../../benchmarks/docs/eval/distribution-comparison.md), and [`benchmarks/docs/profile/README.md`](../../../benchmarks/docs/profile/README.md) cover per-question reports, probability distribution comparisons, and execution timelines.
- Observability: [`observability/README.md`](../../../observability/README.md) and [`observability/runbooks/alerts.md`](../../../observability/runbooks/alerts.md) describe metrics, logs, and troubleshooting. Recording rules are in [`deploy/charts/foretoken/files/recording-rules.yaml`](../../../deploy/charts/foretoken/files/recording-rules.yaml), with dashboards in the adjacent [`grafana/`](../../../deploy/charts/foretoken/files/grafana/) directory.
- Configuration and runnable examples: [`docs/deployment/source.md`](../../../docs/deployment/source.md), [`docs/features/inference-parameters.md`](../../../docs/features/inference-parameters.md), [`examples/recipes/`](../../../examples/recipes/), [`examples/feature/encoder-prefill-decode/`](../../../examples/feature/encoder-prefill-decode/), and [`examples/feature/shared-kv-store/`](../../../examples/feature/shared-kv-store/) cover source deployment, engine parameters, disaggregated services, and cache configuration.
- Engine source and patches: `data-plane/third_party/vllm/` and `third_party/mooncake/` are submodule source entry points. [`.gitmodules`](../../../.gitmodules) lists their repository URLs, and Git gitlinks record the pinned commits. [`data-plane/patches/vllm/`](../../../data-plane/patches/vllm/) contains Foretoken patches; `deploy/inference-engines/*/source-environment.json` records runtime environments and hardware adaptation sources.
- Project history: [Foretoken Issues](https://github.com/shiweijiezero/foretoken/issues), [pull requests](https://github.com/shiweijiezero/foretoken/pulls), and local Git history preserve past discussions, fixes, reversions, and final implementations.

### Finding issues, pull requests, and code

- Issues: search both open and closed issues, and read reproduction conditions, logs, discussions, and final conclusions. Combine model names, operator names, error text, workload characteristics, and performance metrics in queries.
- Pull requests: follow linked PRs from issues to implementation diffs, reviews, measurements, and merge status. After a merge, compare the discussion with the actual code in the target version. Closed, unmerged approaches can also explain useful failure modes and design trade-offs.
- Design discussions: search RFCs, proposals, developer forums, public meeting notes, and talks for approaches not yet implemented and alternatives their designers considered.
- Source and history: read implementations, tests, benchmarks, examples, and release notes, then use commit history to understand why a mechanism was introduced, changed, or reverted.

Example GitHub searches:

```text
repo:vllm-project/vllm is:issue "prefill" "latency"
repo:vllm-project/vllm is:pr is:merged "sampling"
repo:sgl-project/sglang "all-reduce" in:comments
repo:vllm-project/vllm path:vllm "max_num_batched_tokens"
```

The first three queries search issues and pull requests; the last searches code. See the syntax references for [issues and pull requests](https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests), [code](https://docs.github.com/en/search-github/github-code-search/understanding-github-code-search-syntax), and [discussions](https://docs.github.com/en/search-github/searching-on-github/searching-discussions).

Example local searches:

```bash
# Find mechanisms and call sites in knowledge notes, documentation, and code
rg -n -i 'prefill|expert|sampling' rsi-playbooks benchmarks data-plane docs

# Find changes to a mechanism and inspect the actual diff
git log -G 'spec.decode|chunked.prefill' -- data-plane deploy
git show <commit> -- <path>

# Read an upstream issue and its associated implementation discussion
gh issue view <number> --repo vllm-project/vllm --comments
gh pr view <number> --repo vllm-project/vllm --comments
gh pr diff <number> --repo vllm-project/vllm
```

Replace `<commit>`, `<path>`, and `<number>` with the commit, path, and issue or PR number found during your search. Specify the relevant directory explicitly when searching upstream code or experiment results.

### Inference engines and model execution

- vLLM: [Issues](https://github.com/vllm-project/vllm/issues), [PRs](https://github.com/vllm-project/vllm/pulls), [official forum](https://discuss.vllm.ai/), [documentation](https://docs.vllm.ai/), and [blog](https://vllm-project.github.io/). Explore scheduling, chunked prefill, KV caches, model integration, performance regressions, and RFCs. The official forum is the main entry point for community questions.
- SGLang: [Issues](https://github.com/sgl-project/sglang/issues), [PRs](https://github.com/sgl-project/sglang/pulls), [Discussions](https://github.com/sgl-project/sglang/discussions), [documentation](https://docs.sglang.io/), and [Cookbook](https://cookbook.sglang.io/). Explore Radix caching, MoE, speculative decoding, disaggregated deployment, and optimization implementations. The repository's `benchmark/` and `examples/` directories offer measurement and reproduction leads.
- TensorRT-LLM: [Issues](https://github.com/NVIDIA/TensorRT-LLM/issues), [Discussions](https://github.com/NVIDIA/TensorRT-LLM/discussions), [documentation](https://nvidia.github.io/TensorRT-LLM/), and [performance guide](https://nvidia.github.io/TensorRT-LLM/developer-guide/perf-overview.html). Explore NVIDIA backends, attention, quantization, batching, and performance trade-offs.
- Transformers: [Issues](https://github.com/huggingface/transformers/issues), [PRs](https://github.com/huggingface/transformers/pulls), and [documentation](https://huggingface.co/docs/transformers/index). Explore model architectures, configuration, attention masks, RoPE, tokenizers, weight loading, and the history of model support.
- MetaX vLLM: [Issues](https://github.com/MetaX-MACA/vLLM-metax/issues), [source and examples](https://github.com/MetaX-MACA/vLLM-metax), and [documentation](https://vllm-metax.readthedocs.io/en/latest/). Explore MACA runtime requirements, plugin integration, and backend gaps. Join `#sig-maca` through [vLLM Slack](https://slack.vllm.ai) for engineering discussions.

### Distributed serving, routing, and control planes

- Dynamo: [Issues](https://github.com/ai-dynamo/dynamo/issues), [Discussions](https://github.com/ai-dynamo/dynamo/discussions), [documentation](https://docs.nvidia.com/dynamo/latest), and [recipes](https://github.com/ai-dynamo/dynamo/tree/main/recipes). Explore KV-aware routing, planners, prefill/decode (P/D) disaggregation, tiered caching, and failure handling.
- llm-d: [Issues](https://github.com/llm-d/llm-d/issues), [proposals](https://github.com/llm-d/llm-d/tree/main/proposals), [architecture](https://llm-d.ai/docs/architecture), and [deployment paths](https://llm-d.ai/docs/well-lit-paths). Explore scheduling, caches, wide expert parallelism (EP), and inference orchestration on Kubernetes.
- llm-d community: [contributor mailing list](https://groups.google.com/g/llm-d-contributors), [public meeting calendar](https://red.ht/llm-d-public-calendar), and [Slack](https://llm-d.ai/slack). Follow ongoing designs, alternatives, and development plans.
- AIBrix: [Issues](https://github.com/vllm-project/aibrix/issues), [PRs](https://github.com/vllm-project/aibrix/pulls), [router design](https://aibrix.readthedocs.io/latest/designs/aibrix-router.html), and [benchmarks](https://github.com/vllm-project/aibrix/tree/main/benchmarks). Explore load- and prefix-aware routing, LoRA, autoscaling, and request priorities.
- Gateway API Inference Extension: [Issues](https://github.com/kubernetes-sigs/gateway-api-inference-extension/issues) and [design proposals](https://github.com/kubernetes-sigs/gateway-api-inference-extension/tree/main/docs/proposals). Explore InferencePool, EPP, filter/scorer/picker interfaces, and scheduling observability.
- KServe: [Issues](https://github.com/kserve/kserve/issues), [community and meetings](https://github.com/kserve/community), and [design document index](https://github.com/kserve/community/blob/main/design_docs.md). Explore serving APIs, runtime lifecycles, inference graphs, and deployment modes.
- vLLM Production Stack: [Issues](https://github.com/vllm-project/production-stack/issues), [documentation](https://docs.vllm.ai/projects/production-stack), and [tutorials](https://github.com/vllm-project/production-stack/tree/main/tutorials). Explore the integration of engines, routing, caching, and Helm, along with multi-turn request experiments.
- Ray Serve: [Issues](https://github.com/ray-project/ray/issues), [LLM architecture](https://docs.ray.io/en/latest/serve/llm/architecture/index.html), [serving benchmarks](https://docs.ray.io/en/latest/serve/llm/benchmarks.html), and [forum](https://discuss.ray.io/). Explore multi-model serving, replicas, placement, autoscaling, and distributed recovery.

### KV caching, storage, and data transfer

- LMCache: [Issues](https://github.com/LMCache/LMCache/issues), [Discussions](https://github.com/LMCache/LMCache/discussions), [documentation](https://docs.lmcache.ai/), [community meetings](https://docs.lmcache.ai/community/meetings.html), and [benchmark guide](https://docs.lmcache.ai/getting_started/benchmarking.html). Explore KV offloading, tiered storage, P2P, connectors, and measured caching benefits.
- Mooncake: [Issues](https://github.com/kvcache-ai/Mooncake/issues), [documentation](https://kvcache-ai.github.io/Mooncake/), and [FAST25 reproduction materials and traces](https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release). Explore distributed KV caches, the Transfer Engine, RDMA/TCP, and real request traces.
- NIXL: [Issues](https://github.com/ai-dynamo/nixl/issues), [Backend Guide](https://github.com/ai-dynamo/nixl/blob/main/docs/BackendGuide.md), and [NIXLBench](https://github.com/ai-dynamo/nixl/blob/main/benchmark/nixlbench/README.md). Explore memory registration, transfers across memory tiers, asynchronous completion, and message-size-dependent overhead.
- 3FS: [Issues](https://github.com/deepseek-ai/3FS/issues), [design and operational documentation](https://github.com/deepseek-ai/3FS/tree/main/docs), and [benchmarks](https://github.com/deepseek-ai/3FS/tree/main/benchmarks). Explore disaggregated storage, weight reads, random I/O, aggregate throughput, and tail latency.
- Seastar: [source and issues](https://github.com/scylladb/seastar), [tutorial](https://github.com/scylladb/seastar/blob/master/doc/tutorial.md), and [developer mailing list](https://groups.google.com/g/seastar-dev). Explore reactors, per-core execution, backpressure, and I/O scheduling for ideas from other fields that can inform frontend and cache service design.

### Communication, networking, and cross-node coordination

- DeepEP: [source, issues, and PRs](https://github.com/deepseek-ai/DeepEP). Explore MoE dispatch/combine, low-latency communication, SM allocation, topology, and computation/communication overlap.
- NCCL: [Issues](https://github.com/NVIDIA/nccl/issues), [user guide](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/index.html), and [nccl-tests](https://github.com/NVIDIA/nccl-tests). Explore collective algorithms, topology, network plugins, hangs, and multi-node bandwidth.
- RCCL: [documentation](https://rocm.docs.amd.com/projects/rccl/en/latest/) and [rocm-systems, which hosts current source and tests](https://github.com/ROCm/rocm-systems). Explore AMD collective communication, topology, and performance. Historical issues remain searchable in the older `ROCm/rccl` and `ROCm/rccl-tests` repositories.
- UCX: [Issues](https://github.com/openucx/ucx/issues), [documentation](https://openucx.readthedocs.io/en/master/), and [developer mailing list](https://elist.ornl.gov/mailman/listinfo/ucx-group). Explore transport selection, memory registration, RDMA/RoCE, shared memory, and GPU transfers. The source path `src/tools/perf/ucx_perftest` offers measurement leads.
- UCCL: [source and issues](https://github.com/uccl-project/uccl), [project materials](https://uccl-project.github.io/), and [P2P guide](https://github.com/uccl-project/uccl/blob/main/p2p/README.md). Explore CPU-driven networking, congestion control, collective/P2P/EP communication, and execution paths for different NICs.
- Envoy: [Issues](https://github.com/envoyproxy/envoy/issues), [documentation](https://www.envoyproxy.io/docs/envoy/latest/), [Overload Manager](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/operations/overload_manager.html), and [performance framework](https://github.com/envoyproxy/envoy-perf). Explore connection pools, streaming responses, cancellation, timeouts, backpressure, and overload scheduling. The repository README links to meetings and mailing lists.

### Alternative execution architectures and cross-platform backends

- llama.cpp: [Issues](https://github.com/ggml-org/llama.cpp/issues), [Discussions](https://github.com/ggml-org/llama.cpp/discussions), and [generation performance guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/development/token_generation_performance_tips.md). Explore GGUF, CPU/GPU offloading, quantization, Metal/Vulkan/HIP/SYCL backends, and execution trade-offs under memory constraints.
- LMDeploy: [source and issues](https://github.com/InternLM/lmdeploy) and [documentation](https://lmdeploy.readthedocs.io/en/latest/). Explore its TurboMind and PyTorch engines, quantization, KV management, and multimodal execution. The repository's `benchmark/` directory contains measurement implementations.
- FastDeploy: [Issues](https://github.com/PaddlePaddle/FastDeploy/issues), [documentation](https://paddlepaddle.github.io/FastDeploy/), and [benchmarks](https://github.com/PaddlePaddle/FastDeploy/tree/develop/benchmarks). Explore LLMs/VLMs in the Paddle ecosystem, speculative decoding, P/D disaggregation, and integration with a range of Chinese accelerator platforms.
- MLC LLM: [source and issues](https://github.com/mlc-ai/mlc-llm) and [documentation](https://llm.mlc.ai/docs/). Explore compiler-based model deployment, quantization, model libraries, and cross-platform execution on CUDA, ROCm, Vulkan, and WebGPU.
- OpenVINO GenAI: [source and issues](https://github.com/openvinotoolkit/openvino.genai) and [documentation](https://openvinotoolkit.github.io/openvino.genai/). Explore Intel CPU/GPU/NPU execution, continuous batching, speculative decoding, KV eviction, and multimodal pipelines.

### Operators, compilers, and execution runtimes

- PyTorch: [Issues](https://github.com/pytorch/pytorch/issues), [forum](https://discuss.pytorch.org/), and [compiler performance profiling](https://docs.pytorch.org/docs/stable/user_guide/torch_compiler/torch.compiler_profiling_torch_compile.html). Explore graph breaks, recompilation, Inductor, operator selection, Python/launch overhead, and CUDA Graphs.
- Triton: [Issues](https://github.com/triton-lang/triton/issues), [tutorials](https://triton-lang.org/main/getting-started/tutorials/), and [debugging guide](https://triton-lang.org/main/programming-guide/chapter-3/debugging.html). Explore layouts, fusion, generated code, backend differences, autotuning, and compiler optimizations.
- LLVM/MLIR: [source and issues](https://github.com/llvm/llvm-project), [GPU dialect](https://mlir.llvm.org/docs/Dialects/GPU/), [Passes](https://mlir.llvm.org/docs/Passes/), and [forum](https://discourse.llvm.org/). Explore lowering, memory transformations, synchronization, and target instruction generation.
- TVM: [source and issues](https://github.com/apache/tvm), [MetaSchedule](https://tvm.apache.org/docs/deep_dive/tensor_ir/tutorials/meta_schedule.html), and [forum](https://discuss.tvm.apache.org/). Explore schedule search, measurements on target devices, candidate selection, and compilation and deployment.
- CUTLASS/CuTe: [Issues](https://github.com/NVIDIA/cutlass/issues), [Discussions](https://github.com/NVIDIA/cutlass/discussions), and [documentation](https://docs.nvidia.com/cutlass/). Explore GEMM, tiling, pipelines, epilogues, and architecture-specific implementations.
- FlashInfer: [source and issues](https://github.com/flashinfer-ai/flashinfer), [documentation](https://docs.flashinfer.ai/), [community discussions](https://github.com/orgs/flashinfer-ai/discussions), and [benchmarks](https://github.com/flashinfer-ai/flashinfer/tree/main/benchmarks). Explore attention, KV layouts, sampling, dispatch, and graph capture.
- AMD operator libraries: [rocm-libraries](https://github.com/ROCm/rocm-libraries), [hipBLASLt](https://rocm.docs.amd.com/projects/hipBLASLt/en/latest/), [rocBLAS](https://rocm.docs.amd.com/projects/rocBLAS/en/latest/), and [AITER](https://github.com/ROCm/aiter). Explore GEMM algorithm selection, Composable Kernel, shape-specific tuning, and inference operators. Current CK and hipBLASLt development is consolidated in rocm-libraries.
- MetaX operators and programming: [maca-samples](https://github.com/MetaX-MACA/maca-samples), [mcoplib](https://github.com/MetaX-MACA/mcoplib), [mcTriton](https://github.com/MetaX-MACA/mcTriton), and [op_optimization](https://github.com/MetaX-MACA/op_optimization). Explore MACA programming examples, operator implementations, Triton backends, and device integration.

### Hardware architectures, programming guides, and vendor communities

- NVIDIA: [CUDA Programming Guide](https://docs.nvidia.com/cuda/cuda-programming-guide/index.html), [Best Practices](https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/index.html), and the [Hopper](https://docs.nvidia.com/cuda/hopper-tuning-guide/index.html) and [Blackwell tuning guides](https://docs.nvidia.com/cuda/blackwell-tuning-guide/index.html). Explore memory hierarchies, thread scheduling, Tensor Cores, asynchronous copies, and device-specific constraints.
- AMD: [GPU architecture index](https://rocm.docs.amd.com/en/latest/reference/gpu-arch/index.html), [HIP performance guidelines](https://rocm.docs.amd.com/projects/HIP/en/latest/how-to/performance_guidelines.html), and [workload optimization](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/optimization/workload-optimization.html). Explore CDNA, MFMA, registers, LDS, HBM, and multi-chiplet behavior.
- MetaX: [developer documentation center](https://developer.metax-tech.com/doc) and [official source organization](https://github.com/MetaX-MACA). Search programming, communication, tools, and runtime documentation for the relevant chip and SDK version. [mccl_tests](https://github.com/MetaX-MACA/mccl_tests) and [mccl-nccl](https://github.com/MetaX-MACA/mccl-nccl) provide communication implementation and test entry points.
- CPU architectures: [Intel Optimization Reference Manual](https://www.intel.com/content/www/us/en/developer/articles/technical/intel64-and-ia32-architectures-optimization.html) and [AMD Processor Resources](https://www.amd.com/en/developer/browse-by-product-type/processor-resources.html). Explore caches, vector instructions, memory access, processor tuning, and hardware counter definitions.
- Vendor discussions: [NVIDIA Developer Forums](https://forums.developer.nvidia.com/), [ROCm Developer Hub](https://www.amd.com/en/developer/resources/rocm-hub.html), and [ROCm systems discussions](https://github.com/ROCm/rocm-systems/discussions). Explore drivers, tools, device behavior, known issues, and maintainer responses.
- Networking and device health: [NVIDIA Networking documentation](https://docs.nvidia.com/networking/), [DCGM Diagnostics](https://docs.nvidia.com/datacenter/dcgm/latest/learn/modules/dcgm-diagnostics.html), [Linux RDMA](https://github.com/linux-rdma/rdma-core), and [perftest](https://github.com/linux-rdma/perftest). Explore NICs, PCIe/NVLink, NUMA, RoCE, device errors, and low-level bandwidth and latency.
- GPU-initiated communication: [NVSHMEM documentation](https://docs.nvidia.com/nvshmem/index.html), [source](https://github.com/NVIDIA/nvshmem), and [GDRCopy](https://github.com/NVIDIA/gdrcopy). Explore one-sided communication, in-kernel coordination, and the cost of CPU access to mapped GPU memory.

### Profiling and system performance analysis

- NVIDIA: [Nsight Systems](https://docs.nvidia.com/nsight-systems/UserGuide/index.html) covers CPU/GPU timelines, launch gaps, synchronization, and communication overlap; [Nsight Compute](https://docs.nvidia.com/nsight-compute/ProfilingGuide/) covers kernel counters, memory access, occupancy, and roofline analysis.
- PyTorch: [Profiler tutorial](https://docs.pytorch.org/tutorials/beginner/profiler.html) and [memory snapshots](https://docs.pytorch.org/docs/stable/torch_cuda_memory). Connect operators and CPU stacks with device execution and allocator behavior.
- AMD: [Profiling Tools Overview](https://rocm.docs.amd.com/en/latest/components/profiling-tools-overview.html), [rocprofv3](https://rocm.docs.amd.com/projects/rocprofiler-sdk/en/latest/how-to/using-rocprofv3.html), [Compute Profiler](https://rocm.docs.amd.com/projects/rocprofiler-compute/en/latest/), and [Systems Profiler](https://rocm.docs.amd.com/projects/rocprofiler-systems/en/latest/). Current source is consolidated in [rocm-systems](https://github.com/ROCm/rocm-systems).
- MetaX: [performance tools index](https://developer.metax-tech.com/doc?category_ids=61). Choose mcTracer and mcProfiler manuals matching the SDK for capture, process attachment, MCTX markers, timelines, and hardware metrics.
- Perfetto: [SQL analysis introduction](https://perfetto.dev/docs/analysis/perfetto-sql-getting-started) and [Trace Processor CLI](https://perfetto.dev/docs/reference/trace-processor-cli). Learn to extract stage durations, overlap, and waiting from raw traces for repeatable analysis.
- Linux: [Perf Wiki](https://perfwiki.github.io/main/tutorial/), [cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html), and [NUMA memory policy](https://docs.kernel.org/admin-guide/mm/numa_memory_policy.html). Explore CPU sampling, scheduling, throttling, memory pressure, and NUMA placement.
- CPU profilers: [Intel VTune](https://www.intel.com/content/www/us/en/docs/vtune-profiler/user-guide/2026-0/analysis-types.html) and [AMD uProf](https://docs.amd.com/r/en-US/57368-uProf-user-guide). Explore host pipelines, cache misses, memory bandwidth, and remote NUMA accesses.
- Performance analysis methods: [NERSC Roofline](https://docs.nersc.gov/tools/performance/roofline/), [Empirical Roofline Toolkit](https://bitbucket.org/berkeleylab/cs-roofline-toolkit/), and [Brendan Gregg's USE Method](https://www.brendangregg.com/usemethod.html). Explore measured compute and bandwidth ceilings, and systematic analysis of resource utilization, saturation, and errors.

### Paper searches, citation trails, and formal publications

- [arXiv](https://arxiv.org/search/advanced): discover work by title, abstract, author, and date; inspect version history, publication information, and code links. For inference systems, search across categories such as `cs.DC`, `cs.PF`, `cs.AR`, `cs.DB`, and `cs.LG`.
- [OpenReview](https://openreview.net/): read papers, public reviews, author responses, and revisions to understand baseline selection, ablations, applicability, and points of disagreement.
- [DBLP](https://dblp.org/): verify exact titles, authors, venues, and published versions, and follow author and venue indexes for related work over time.
- [Semantic Scholar](https://www.semanticscholar.org/): follow references, citing papers, and recommendations to find underlying assumptions, subsequent improvements, and independent reevaluations.
- Author and lab websites: follow authors to their publications, projects, and blogs, such as [Tri Dao](https://tridao.me/) and [Hao AI Lab](https://haoailab.com/). Look for code, slides, follow-up research, and explanations beyond the paper.
- Surveys and reevaluations: [A Survey of LLM Inference Systems](https://arxiv.org/abs/2506.21901), for example, helps classify mechanisms; a [reevaluation of disaggregated inference performance and energy use](https://arxiv.org/abs/2601.08833) reveals limits imposed by workloads and transfer media. Follow citations for newer developments and contrary results.

### Conference papers, talks, and reproduction materials

- ML systems and model algorithms: [MLSys](https://proceedings.mlsys.org/), [NeurIPS](https://proceedings.neurips.cc/), [ICML/PMLR](https://proceedings.mlr.press/), and [ICLR/OpenReview](https://openreview.net/). Explore serving, compilers, runtimes, model architectures, speculative decoding, sparsity, and compression.
- Systems and networking: [OSDI](https://www.usenix.org/conference/osdi25/technical-sessions), [NSDI](https://www.usenix.org/conference/nsdi26/technical-sessions), [SOSP](https://sigops.org/s/conferences/sosp/2025/accepted.html), and [SIGCOMM](https://conferences.sigcomm.org/sigcomm/2025/program/papers-info/). Explore disaggregated architectures, KV caches, scheduling, resource isolation, RDMA, and multi-node serving. Follow each year's program to papers, talks, slides, and artifacts.
- Computer architecture: [ASPLOS](https://www.asplos-conference.org/asplos2026/program/), [ISCA](https://www.iscaconf.org/isca2026/program/), [MICRO](https://microarch.org/micro58/program/index.php), and [HPCA](https://2026.hpca-conf.org/program/program-hpca-2026/). Explore hardware/software co-design, memory hierarchies, interconnects, accelerators, and dataflow.
- Parallel computing and HPC: [PPoPP](https://ppopp25.sigplan.org/) and [SC](https://sc25.supercomputing.org/program/papers/). Explore synchronization, parallel runtimes, compilation, topology, and communication at scale.
- Artifacts and experiment supplements: [OSDI Artifact](https://www.usenix.org/conference/osdi26/call-for-artifacts), [SC reproducibility initiative](https://sc25.supercomputing.org/program/papers/reproducibility-initiative/), and [PPoPP Artifact](https://ppopp25.sigplan.org/track/PPoPP-2025-artifact-evaluation). Follow papers to pinned versions, environments, scripts, raw results, evaluation records, and reproduction instructions.

### Technical blogs, model teams, and public courses

- Framework engineering blogs: [LMSYS](https://lmsys.org/blog/), [PyTorch](https://pytorch.org/blog/accelerating-moe-model/), [LMCache](https://blog.lmcache.ai/), and [FlashInfer](https://flashinfer.ai/). Study execution mechanisms, implementation details, performance evidence, and remaining questions, then follow the associated code.
- Vendor technical material: [NVIDIA Technical Blog](https://developer.nvidia.com/blog/) and [ROCm Blog](https://rocm.blogs.amd.com/). Explore engineering case studies on new hardware, kernels, communication, graph execution, and profilers.
- Research teams and cloud serving practice: [Google Research](https://research.google/pubs/), [Microsoft Research's Splitwise](https://www.microsoft.com/en-us/research/blog/splitwise-improves-gpu-usage-by-splitting-llm-inference-phases/), [Together AI's Flash-Decoding](https://www.together.ai/blog/flash-decoding-for-long-context-inference), and [Hao AI Lab's DistServe retrospective](https://haoailab.com/blogs/distserve-retro/). Explore phase separation, heterogeneous resources, algorithm/system co-design, and design changes as research moves into serving.
- Model teams' systems material: [DeepSeek Open Infra](https://github.com/deepseek-ai/open-infra-index) and [DeepSeek-V3](https://github.com/deepseek-ai/DeepSeek-V3). Explore model reports, inference architectures, DeepEP, DeepGEMM, FlashMLA, expert load balancing, and profiling data.
- Architecture research community: [SIGARCH](https://www.sigarch.org/tag/machine-learning-systems/). Look for cross-disciplinary questions, research perspectives, workshops, and hardware/software co-design ideas.
- Public lectures: [Stanford MLSys Seminar](https://mlsys.stanford.edu/), [GPU MODE lecture materials](https://github.com/gpu-mode/lectures), and [GPU MODE videos](https://www.youtube.com/@GPUMODE). Study profiling, Triton, quantization, communication, multi-GPU execution, compilation, and numerical behavior alongside the corresponding code and slides.
- Developer conferences: [Triton Developer Conference materials](https://github.com/triton-lang/triton/blob/main/docs/meetups/dev_conference_2025.md). Follow the agenda to talks, slides, and implementations to understand backend, compiler, and kernel design trade-offs.

### Design spaces, simulation, and architectural innovations

- [FastAFD](https://github.com/hao-ai-lab/FastAFD): explore attention/FFN disaggregation, communication overlap, alignment validation, and experiment scripts, considering the required hardware and execution conditions.
- [Vidur](https://github.com/microsoft/vidur): explore profiling-based inference simulation, request traces, and configuration exploration methods.
- [AFD-Ledger](https://github.com/kvcache-ai/AFD-Ledger): explore disaggregated deployment analysis under hardware mix, device memory, TPOT, and budget constraints.

For architectural approaches, combine related terms such as `attention FFN disaggregation`, `disaggregated expert parallelism`, and `operator-level disaggregation`. Expand into parallel computing, databases, networking, and architecture with terms such as `dataflow`, `locality`, `pipeline scheduling`, `communication avoidance`, `heterogeneous scheduling`, and `memory disaggregation`.

### Models, quantization, speculative decoding, and attention implementations

- Hugging Face model pages and files: examples include the [Qwen3-Next model card](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct), its [config.json](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct/blob/main/config.json), the [DeepSeek-V3 model card](https://huggingface.co/deepseek-ai/DeepSeek-V3), and its [config.json](https://huggingface.co/deepseek-ai/DeepSeek-V3/blob/main/config.json). Explore architectures, hybrid attention, MoE, MLA, MTP, quantization, RoPE, tokenizers, and chat templates. Read the target model's actual configuration to understand its execution requirements.
- Model communities: [Hugging Face Forums](https://discuss.huggingface.co/) and model pages' Community tabs, such as [Qwen3-Next Discussions](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct/discussions). Look for reproductions and responses concerning model/runtime compatibility, device memory, speculative model configuration, and unexpected outputs.
- Quantization: [llm-compressor](https://github.com/vllm-project/llm-compressor) and [NVIDIA Model Optimizer](https://github.com/NVIDIA/Model-Optimizer). Explore how calibration, low-bit weights and KV caches, deployment formats, and accuracy relate to the kernels actually used.
- Speculative decoding: [Dynamic Speculation](https://huggingface.co/blog/dynamic_speculation_lookahead), [SPEED-Bench data](https://huggingface.co/datasets/nvidia/SPEED-Bench), and [implementation guide](https://github.com/NVIDIA-NeMo/Gym/blob/main/benchmarks/speed-bench/README.md). Explore how adaptive lookahead, acceptance rates, draft costs, and task distributions affect gains.
- Attention and linear attention: [FlashAttention](https://github.com/Dao-AILab/flash-attention) and [Flash Linear Attention](https://github.com/fla-org/flash-linear-attention). Explore attention kernels, KV caches, linear attention, state updates, hybrid models, and inference implementations.
- Operator design languages: [TileLang](https://github.com/tile-ai/tilelang) and [documentation](https://tilelang.com/). Explore how to write GEMM, quantization, attention, layouts, pipelines, and autotuning.
- Ascend backend: [vLLM Ascend Issues](https://github.com/vllm-project/vllm-ascend/issues), [documentation](https://docs.vllm.ai/projects/ascend/en/latest/), and [support matrix](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/support_matrix/). Explore the specific requirements for Ascend model execution, parallelism, and operator integration.

### Real workloads and long-context data

- [Azure Public Dataset](https://github.com/Azure/AzurePublicDataset) and [LLM Inference Dataset 2024](https://github.com/Azure/AzurePublicDataset/blob/master/AzureLLMInferenceDataset2024.md): explore request timing and input/output length distributions. This trace supplies timestamps and token counts for studying arrivals and lengths; choose a separate dataset for text content.
- [BurstGPT](https://github.com/hpmll/BurstGPT): explore bursts, session relationships, request failures, filtering, and their effects on serving workloads.
- [LMSYS-Chat-1M](https://huggingface.co/datasets/lmsys/lmsys-chat-1m): explore real multi-turn content and task and language distributions. Study arrivals separately using timestamped traces.
- [Mooncake FAST25 data and experiments](https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release): explore real workloads involving input/output lengths, shared prefixes, and cache behavior.
- [RULER](https://github.com/NVIDIA/RULER) and [LongBench](https://github.com/THUDM/LongBench): explore effective context length, retrieval, multi-hop reasoning, aggregation, long-document and code tasks, and generation and scoring scripts.
- [MMLongBench](https://github.com/EdinburghNLP/MMLongBench): explore Visual RAG, PDF summarization, document question answering, and long multimodal contexts to understand the combined costs of visual encoding, image tokens, and text.

### Public performance reports, evaluators, and reproduction scripts

- MLPerf: [reference implementations](https://github.com/mlcommons/inference), [example public submissions](https://github.com/mlcommons/inference_results_v5.1), and [AMD reproduction guide](https://rocm.blogs.amd.com/artificial-intelligence/mlperf-inference5.1-repro/README.html). Explore standardized workloads, LoadGen, hardware/software configurations, code, and measurements. Choose submissions matching the version under study.
- [GuideLLM](https://github.com/vllm-project/guidellm) and [AIPerf](https://github.com/ai-dynamo/aiperf): explore concurrency, request rates, multi-turn workloads, real traces, multimodal workloads, client timing implementations, machine-readable results, and telemetry.
- [InferenceX](https://github.com/SemiAnalysisAI/InferenceX): follow public scores to hardware, engines, collective/operator configurations, and execution materials. The project was formerly named InferenceMAX.
- [Independent engine reproductions](https://github.com/Michaelvll/llm-ie-benchmarks): explore vLLM/SGLang deployment configurations, benchmarks, and raw logs to understand how sample counts and workloads affect comparisons.
- [Artificial Analysis methodology](https://artificialanalysis.ai/methodology): explore definitions for user-facing service TTFT, output speed, reasoning, caching, and tokenization.
- Performance disputes and measurement fixes: examples include [unreproduced vLLM speculative speedups](https://github.com/vllm-project/vllm/issues/10318), [slower quantized drafts](https://github.com/vllm-project/vllm/issues/21278), and [SGLang long-context comparisons](https://github.com/sgl-project/sglang/issues/3471). Read the actual environments, commands, failure conditions, and subsequent conclusions.
- Evaluation input pipelines: [vLLM PR #25916](https://github.com/vllm-project/vllm/pull/25916) fixes the effect of duplicate BOS tokens on EAGLE3 acceptance rates. Such PRs provide direct evidence of how chat templates, tokenization, and client behavior change measurements.

### Clients, frontends, and model loading

- Rust asynchronous execution: [Tokio's synchronous/asynchronous boundary](https://tokio.rs/tokio/topics/bridging) and [tracing](https://tokio.rs/tokio/topics/tracing). Explore runtime blocking, concurrent tasks, and span correlation to explain frontend/router waits.
- Python: [asyncio development guide](https://docs.python.org/3/library/asyncio-dev.html). Explore slow callbacks, event-loop stalls, thread interaction, and debugging.
- Streaming protocols and proxies: [SSE specification](https://html.spec.whatwg.org/multipage/server-sent-events.html) and [Envoy flow control](https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/flow_control). Explore event boundaries, buffering, and HTTP/2 flow control to explain timing differences between engine output and client receipt.
- CPU inference: [vLLM CPU documentation](https://docs.vllm.ai/en/stable/getting_started/installation/cpu/). Explore OpenMP, physical cores, NUMA, rank placement, and competition among serving threads.
- Downloads and storage: [Hugging Face model downloads](https://huggingface.co/docs/hub/models-downloading) and [fio](https://github.com/axboe/fio). Explore Xet, concurrent downloads, caching, lazy loading, and independent I/O measurements, treating downloads, weight loading, and inference as separate stages.
- Broader public discussions: [Hacker News Search](https://hn.algolia.com/) can surface project announcements, engineering retrospectives, and author discussions. Follow their links to papers, code, and reproducible materials.
