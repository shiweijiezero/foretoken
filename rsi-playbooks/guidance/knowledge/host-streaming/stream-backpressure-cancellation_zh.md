<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 限制流式资源占用：背压传播与取消清理

[English](stream-backpressure-cancellation.md) | 简体中文 · [推理知识](../README_zh.md)

## 现象与动机

稳定的引擎 token 产出，经文本事件队列、响应 body、代理和传输窗口后，可能变成客户端突发到达。Enqueue 或 write 成功只表示本层接收了数据。缺少有界缓冲和取消 ownership 时，慢客户端或断开的连接仍可能占用健康请求需要的主机内存、引擎计算与 KV。

## 原理与分析

队列占用 B、生产速率 p、消费速率 c 满足 `dB/dt=p−c`。恒定速率且 p > c 时，容量 K 约在 `(K−B₀)/(p−c)` 后饱和。扩大缓冲只是延迟反馈，不消除持续速率差，下游每一级队列还会进一步延后背压。

SSE 用 UTF-8 行分帧，遇空行才分发事件，不是一个 token 一个包。HTTP/2 同时有 stream 与 connection 流控；完整 body filter 需要缓冲，streaming filter 可增量转发。连接断开、abort 接收、引擎终止、KV 释放是不同事件；取消后以速率 r 继续执行 Δ 时间，约浪费 rΔ 个 token。

## 设计

响应流 ownership 与稳定的引擎请求身份相连。下游需求控制上游 poll 或有界队列；独立无界接收任务会切断背压路径。传输流控限制缓冲，引擎取消结束资源使用，不能让慢 socket 无限期阻塞共享设备 scheduler。

Heartbeat 注释维持连接，但不重置请求 deadline 或 backend idle。Foretoken 的可取消输出流、model-server abort 和 idle 清理提供对应职责边界。

## 实现

应用侧状态流如下：

```text
请求活跃时：
    等待下游可写、断开、deadline 或 backend idle
    下游可写：
        poll 上游，封装事件，等待响应写入
        等待期间仍监听断开/deadline/backend idle 并触发取消
    断开或超时：
        停止 poll，用请求身份发送 engine abort
        终止响应并取消自己持有的转发任务
        退出
引擎 scheduler 处理 abort：
    后续 batch 不再纳入该请求
    退役在途工作，安全后释放请求状态与 KV
```

生产与写入分属任务时使用有界 channel，并让任一端关闭触发该路径的取消。Abort 应答不是设备完成 fence，已提交内核可能先完成再清理。Envoy 的暂停读取和缓冲上限会传播代理背压，完整 body filter 会改变这一行为。SSE 解析必须跨任意网络读取累积完整事件。

## 影响与效果

有界队列限制本地主机缓冲，并暴露慢消费者。取消减少未来请求工作，最终释放 KV，而非仅关闭 HTTP，在频繁断开时为健康请求保留容量。代价是跨阶段协调和最后一步在途计算；只有 socket 背压不能证明生成已停止。HTTP/2 connection 级压力还会影响共享连接的其他流。

## 参考

- [WHATWG SSE 规范](https://html.spec.whatwg.org/multipage/server-sent-events.html)：UTF-8、逐行解析、空行分发、注释和重连。EventSource 的重连语义不能直接当成所有 LLM HTTP 客户端的自动请求重放。
- [Envoy 流控文档](https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/flow_control)：listener、cluster、stream 上限及缓冲 filter。
- [Foretoken 共享流处理](../../../../data-plane/frontend/src/server/src/api/stream.rs)与[HTTP model facade](../../../../data-plane/frontend/src/llm-facade/src/http.rs)：`e6ae3df6` 的 heartbeat、idle 清理，以及 abort 接收与最终终止的职责区别。
- [Foretoken 指标定义](../../../../benchmarks/docs/metrics_zh.md)：客户端分片间隔和成功请求计时，不暴露 backend 资源释放时间。
