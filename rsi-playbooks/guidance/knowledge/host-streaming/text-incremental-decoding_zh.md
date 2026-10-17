<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少流式文本处理开销：增量解码与后缀输出

[English](text-incremental-decoding.md) | 简体中文 · [推理知识](../README_zh.md)

## 现象与动机

token ID 不是独立可显示的文本片段。字节级 tokenizer 会拆开 Unicode 字符，decoder 规则可能依赖前文，stop string 和工具解析器还会扣留已计算文本。每步重解码整个前缀，也会使长输出的主机成本持续增长，即使设备节奏稳定。

## 原理与分析

解码成本与输入长度线性相关时，依次解码长度 1 到 n 的前缀需要 `Σj=O(n²)` 工作。增量解码只处理新增 token 与边界上下文；反复构造累计字符串或重放 parser 仍可能带来增长成本。

排除最大长度 L 的 stop string，需要在继续生成时保留至多 L−1 个字符，避免未来 stop marker 包含已发文本。分组 k 个 token 可将固定输出成本 a 摊销为 a/k，但在匀速间隔 d 下增加平均等待 `(k−1)d/2`。这是解释性模型，不是实测加速。

## 设计

每请求保存 decoder 上下文、稳定文本 offset、待决 stop 后缀与 parser 状态。只输出新增稳定文本，保留尚未确定的后缀。正常结束发送全部合法残余文本，包括不足一组的输出；取消则丢弃待发内容并释放状态。

复用 tokenizer 支持的增量 decoder，不逐 token 独立解码。兼容请求可以共用 batch decode，但 offset、stop 规则和结束状态各自维护。

## 实现

核心更新过程如下：

```text
收到新 token ID：
    推进增量 decoder，取得新增稳定文本
    追加到待决后缀
    检索 stop string，包含旧后缀与新文本交界的匹配
    若匹配：
        输出允许前缀，仅在配置要求时包含 stop 文本
        结束请求
    否则：
        输出待决文本中最后 L−1 个字符之前的部分
        保留该后缀；L=0 时不保留
正常结束：完成 decoder，应用 stop 规则，flush 合法残余
取消：丢弃待发文本，释放 decoder/parser 状态
```

vLLM 提供原生 `DecodeStream` 与 helper 增量路径及 delta 输出；SGLang 维护 surrogate/read offset 和延迟分片。Fastokens 适用于部分 BPE 路径，文档注明从 vLLM 0.23.0 起支持且要求 fastokens ≥0.2.0。推理内容和工具 parser 可能需要更多上下文。Foretoken 按非空 `choices` 记录接收分片时间，role／finish 也计入，不是逐 token 计时。

## 影响与效果

增量解码消除完整前缀的重复转换，后缀输出避免反复复制累计字符串。Stop 缓冲保证文本正确，但会产生与内容相关的可见延迟。分组发送降低每事件固定成本，同时增加等待。共享输出队列饱和时，更多 decoder worker 无法解决瓶颈；分片更大也可能让观测 ITL 上升，而 token 吞吐改善。

## 参考

- [vLLM 增量 detokenizer](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/engine/detokenizer.py)：原生/helper 路径、stop 缓冲与 delta 输出。
- [SGLang detokenizer manager](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/managers/detokenizer_manager.py)：增量 offset、延迟文本、分组解码与结束清理。
- [vLLM 优化文档](https://docs.vllm.ai/en/latest/configuration/optimization/)：fastokens 版本和 tokenizer 范围。
- [Foretoken 分片计时](../../../../benchmarks/src/benchmarks/integrations/streaming.py)与[指标定义](../../../../benchmarks/docs/metrics_zh.md)：`e6ae3df6` 的实现，ITL 描述接收到的分片间隔。
