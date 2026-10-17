<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 降低首个可播放媒体延迟：分块解码与交付

[English](media-chunk-decoding.md) | 简体中文

## 现象与动机

文本 token 流不等于可播放的语音或视频流。波形解码器可能需要未来编码块，视频 VAE 可能等待 latent 块，网络字节到达时播放器也可能还不能使用。分块生成优化首个可用媒体和连续播放，不只是首个生成编码。

## 原理与分析

流水线让阶段重叠：Thinker 提供文本和隐藏状态条件，Talker 输出 codec 块，Code2Wav 解码当前块时 Talker 继续生成下一块。解码器 lookahead 决定启动下界。Qwen2.5-Omni 波形路径使用 flow-matching DiT 与修改后的 BigVGAN，DiT 每块读取两个历史块、当前块和一个未来块。这属于 Qwen2.5 的设计，不能直接套到 Qwen3。

每块含 `D` 秒媒体，每 `A` 秒到达一块，持续播放要求平均 `A≤D`。`A<D` 才有延迟后补充缓冲的余量，但足够长的尾部间隔仍会导致播放中断。

## 设计

跨异步阶段保留请求关联、块序和允许访问的历史。Orchestrator 协调取消，在消费者完成后释放 buffer。有限的生产／消费队列可以控制慢客户端积压；引用架构没有规定统一的 credit 协议。

vLLM-Omni 的 Qwen3-Omni Thinker→Talker→Code2Wav 与扩散视频输出是两条路径。后者入口为 `WS /v1/realtime/video`，先发送 `video.chunk_metadata` JSON，紧接着发送对应 fragmented MP4 二进制块，格式为 `m4s`。它用于生成视频输出，不是实时对话视频输入。

## 实现

逐块解码与交付流程为：

```text
以同一请求标识启动各生成阶段
生成未结束且请求仍活跃时:
    获取带顺序号的下一codec或latent块
    等待模型所需的未来块到齐
    使用兼容历史及解码权重生成媒体
    封装可播放音频或fragmented-MP4
    视频先发metadata，再发对应binary frame
    保留下一块所需历史，释放已消费buffer
取消时停止各阶段，结束其拥有的未完成传输
```

使用匹配的公开文本、codec、波形和 VAE 资产。交付本身无需新训练，[少步因果蒸馏](few-step-distillation_zh.md)是另一项模型变化。模型必须支持增量解码，扩散 pipeline 必须实现 step execution 与块输出；streaming 参数不能改变最终输出模型的能力。Vocoder/VAE 精度和 CUDA 依赖可能不同于 BF16/FP16 transformer。

## 影响与效果

分块省掉了“生成完整回答后才能开始解码和交付”的等待，阶段重叠减少串行关键路径。Lookahead、初始条件、VAE／vocoder 和封装仍决定首块何时可播放；codec 提前不能单独消除这些成本。

小块降低缓冲延迟，却增加块边界、启动和封装开销。减少模型 lookahead 或截断历史还会改变语音连续性、视频运动和主体；单纯传输分块则保留模型语义。慢客户端需要消费进度约束生产，否则字节队列增长。平均实时因子良好，突发交付仍可能造成播放中断。

## 参考

- [Qwen2.5-Omni，arXiv:2503.20215v1](https://arxiv.org/html/2503.20215v1)：语音架构与块级 lookahead；报告里的视频是输入，不是生成输出。
- [Omni 架构](https://github.com/vllm-project/vllm-omni/blob/37f094a388ba1c79f83c129fda39797c1ebfc2a4/docs/design/architecture_overview.md)与[视频 API](https://github.com/vllm-project/vllm-omni/blob/37f094a388ba1c79f83c129fda39797c1ebfc2a4/docs/serving/streaming_video_output_api.md)：`37f094a` 内容；这些是已有 upstream 路径，受模型限制，`v0.30.0` 发布早于该 main 版本。
