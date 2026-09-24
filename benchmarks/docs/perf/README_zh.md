# 测量服务性能

[English](README.md) | 简体中文 · [评测与性能剖析](../../README_zh.md)

使用 `foretoken perf`，比较不同负载下的响应延迟、token 生成速度和吞吐量。

## 准备

完成[通用准备](../../README_zh.md#开始使用)后，在仓库根目录执行命令。示例均使用 `examples/quickstart`。

评测已有端点时，将目录换成 `--url "$MODEL_SERVICE_URL" --model "$MODEL_ID"`。默认模式下已部署的快速开始示例可这样获取地址和模型名：

```bash
MODEL_SERVICE_BASE_URL="$(foretoken endpoint examples/quickstart)"
export MODEL_SERVICE_URL="${MODEL_SERVICE_BASE_URL%/}/v1/chat/completions"
export MODEL_ID=Qwen/Qwen3-0.6B
```

其他服务使用其实际 Chat Completions URL 和模型名。Gateway 访问和 HTTP 参数扫描使用 Kustomize 目录；Foretoken 会从中查找 Gateway 路由请求头。

## 命令分类

| 要做什么 | 示例 |
| --- | --- |
| 运行简单负载 | [固定提示词](fixed-prompt_zh.md)、[非流式请求](non-streaming_zh.md) |
| 控制输入和输出长度 | [随机负载](random_zh.md) |
| 使用真实对话 | [本地对话](conversations_zh.md)、[Hugging Face 数据集](huggingface_zh.md)、[ShareGPT](sharegpt_zh.md)、[工具数据](tools_zh.md) |
| 混合数据集或控制请求到达 | [多数据集](multi-dataset_zh.md)、[请求速率与并发](arrival-rate_zh.md) |
| 回放历史流量 | [StudyChat](studychat_zh.md)、[Mooncake trace](mooncake-trace_zh.md) |
| 比较负载设置 | [参数扫描](sweep_zh.md)、[SLO 并发搜索](slo_zh.md) |
| 测量视频生成性能 | [视频负载](video_zh.md) |
| 比较运行和查看图表 | [W&B 输出](wandb_zh.md) |

指标定义见[性能指标](../../metrics_zh.md)，全部参数见 `foretoken perf --help`。需要定位执行瓶颈时，在负载运行的同时[采集 profile](../profile/README_zh.md)。
