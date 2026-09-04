# 常用评测命令

[English](examples.md) | 简体中文

## 准备

按[模型服务性能评测](../README_zh.md)安装评测客户端，并按[快速开始](../../README_zh.md#快速开始)准备集群。以下命令在仓库根目录执行。首次使用 W&B 请先运行 `wandb login`。

各命令使用 `examples/quickstart`。评测其他已有服务时，将该路径换成 `--url "$MODEL_SERVICE_URL" --model "$MODEL_ID"`。默认模式下已部署的快速开始示例可以这样获取地址：

```bash
MODEL_SERVICE_BASE_URL="$(foretoken endpoint examples/quickstart)"
export MODEL_SERVICE_URL="${MODEL_SERVICE_BASE_URL%/}/v1/chat/completions"
export MODEL_ID=Qwen/Qwen3-0.6B
```

其他服务使用其实际 Chat Completions URL 和模型 ID。Gateway 模式传入部署配置目录，由 Foretoken 配置路由请求头。参数扫描也必须传入该目录，不支持 `--url`。

## 命令分类

- [固定提示词](coomon_commands/fixed-prompt_zh.md)
- [非流式请求](coomon_commands/non-streaming_zh.md)
- [随机负载](coomon_commands/random_zh.md)
- [本地对话数据](coomon_commands/conversations_zh.md)
- [Hugging Face 数据集](coomon_commands/huggingface_zh.md)
- [ShareGPT 对话](coomon_commands/sharegpt_zh.md)
- [工具数据](coomon_commands/tools_zh.md)
- [多数据集](coomon_commands/multi-dataset_zh.md)
- [请求速率与并发](coomon_commands/arrival-rate_zh.md)
- [StudyChat 轨迹回放](coomon_commands/studychat_zh.md)
- [Mooncake trace 回放](coomon_commands/mooncake-trace_zh.md)
- [参数扫描](coomon_commands/sweep_zh.md)
- [视频生成评测](coomon_commands/video_zh.md)
- [W&B 输出](coomon_commands/wandb_zh.md)

指标定义见[结果指标](../metrics_zh.md)。全部参数见 `foretoken bench --help`。
