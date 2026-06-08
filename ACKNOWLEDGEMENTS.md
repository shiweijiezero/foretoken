# 致谢

Foretoken 构建于一系列开源项目、公开数据集与公开方法之上。在此向其作者与贡献者致谢。

## 推理引擎

- **[vLLM](https://github.com/vllm-project/vllm)** — 本项目的工业级推理底座。Foretoken 全程经 vLLM 官方扩展点接入(零 fork),并在进程内自起 `AsyncLLM` 做闭环回放评测。

## 评测负载来源

Foretoken 的评测数据集 [`weijiezz/foretoken-trace`](https://huggingface.co/datasets/weijiezz/foretoken-trace) 由以下公开来源缝合而成——时序 / 并发 / 会话结构与真实多轮对话内容分别取自:

- **Mooncake trace**(Moonshot AI / Kimi,经 [`valeriol29/mooncake-traces`](https://huggingface.co/datasets/valeriol29/mooncake-traces) 公开)— 提供真实的到达时序、并发与会话结构(由 hash 前缀延续链重建会话)。
- **[LightSeek `kimi-mtp-dataset`](https://huggingface.co/datasets/lightseekorg/kimi-mtp-dataset)** — 提供真实多轮对话内容,按会话逐轮填入。

两者内容与时序均保留各自真实形态;Foretoken 仅做会话重建与内容填充,不复刻其缓存量化粒度(见 `docs/07`)。派生数据集遵循各上游来源的许可证。

## 方法与概念

- **goodput / SLO 达成阶梯** — 以满足延迟 SLO 的有效 token 吞吐衡量「可用产能」,而非原始吞吐。
- **MTP(Multi-Token Prediction)** 与 **价值感知 KV 管理** — Foretoken 优化与评测的对象。

## 受启发的开源实践

工程与文档规范参考了 **[vLLM](https://github.com/vllm-project/vllm)** 与 **[TokenSpeed](https://github.com/lightseekorg/tokenspeed)** 的开源实现(模块结构、文件头 SPDX、致谢与归属)。
