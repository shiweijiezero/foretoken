<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少设备空闲间隙：重叠主机准备与设备执行

[English](host-device-overlap.md) | 简体中文 · [推理知识](../README_zh.md)

## 现象与动机

短解码步骤可能在等待主机选择请求、构造元数据、提交内核和接收结果时让设备空闲。更快的内核会暴露这些工作。HTTP handler 即使写成 `async`，在挂起前同步分词或解析，也会阻塞其他连接。设备提交与 HTTP 响应性是两个不同瓶颈。

## 原理与分析

设主机准备耗时 H、设备执行耗时 D，串行稳态间隔为 H + D；若下一步准备独立于当前输出，重叠后的理想间隔接近 max(H, D)，再加未覆盖的同步与结果处理。作为解释性算例，H = 2 ms、D = 3 ms 时，串行间隔为 5 ms，理想重叠为 3 ms。这不是完整请求延迟。

普通 Python 字节码线程受 GIL 约束；asyncio 不会抢占同步 callback，即使底层扩展释放了 GIL；下一步设备输入还可能依赖本步采样 token。线程池能改变主机执行位置，不能解除最后一种数据依赖。

## 设计

调度与在途请求状态由引擎 scheduler 持有。当前步骤运行时准备独立元数据；后端支持时在设备上接力依赖 token，随后将实际输出与预留 placeholder 结算。抢占和取消先使对应状态失效，迟到结果不能再参与后续调度。

阻塞渲染或分词使用已有执行器路径。执行器排队和 CPU 服务耗时分别处理，线程并发不承担另一套隐式准入策略。

## 实现

核心流水如下：

```text
为步骤 s 预留请求槽位和输出 placeholder
提交 s，发布设备状态句柄与完成事件
s 运行时准备 s+1 的独立元数据
s+1 消费依赖状态前：
    在消费 stream 上等待生产者事件
    解析 token/状态引用，避免不必要的 CPU 拷贝
收到输出时：
    仅为仍活跃的请求代次结算 placeholder
    丢弃抢占或取消后的过期结果
最后一个设备消费者完成后再释放 buffer
```

vLLM `AsyncScheduler` 管理 placeholder 与输出结算，SGLang overlap 工具负责按请求发布状态和消费者 fence。部分 backend 仍需 CPU 序列长度副本。vLLM online renderer 为非流式、普通流式和带解析流式提供执行器包装。自由线程 CPython 不会移除原生锁或这些依赖，其扩展兼容性决定实际 GIL 状态。

## 影响与效果

流水化将独立主机工作隐藏在设备执行后，减少暴露的提交间隙。执行器隔离避免一个同步 callback 延迟其他 HTTP 流，但不会减少 CPU 总工作。在途 buffer、结果结算、执行器排队和流水填充都会增加成本。设备已主导耗时，或每次决策都需要最新 CPU 可见输出时，可隐藏的主机时间有限。

## 参考

- [Python 3.14 asyncio 开发文档](https://docs.python.org/3.14/library/asyncio-dev.html)与[自由线程文档](https://docs.python.org/3.14/howto/free-threading-python.html)：协作式调度、执行器与扩展兼容性。
- [vLLM AsyncScheduler](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/sched/async_scheduler.py)与[online renderer](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/renderers/online_derenderer.py)。
- [PR #49396](https://github.com/vllm-project/vllm/pull/49396)与[Issue #57350](https://github.com/vllm-project/vllm/issues/57350)：renderer 执行器集成。
- [SGLang overlap 工具](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/managers/overlap_utils.py)：不同 backend 的同步方式并不相同。
