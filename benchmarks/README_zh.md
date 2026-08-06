# Benchmarks

[English](README.md) | 简体中文

`benchmarks/` 是 Foretoken 的评测模块。

它对已部署的推理服务发请求、测性能、比配置，并检查回答质量是否达标。目标是用可复现的实验，回答「这个服务能不能稳住延迟和吞吐，质量够不够好」。

## 什么时候需要它

- 想知道当前服务在某个并发或到达率下的延迟和吞吐。
- 想对比不同并发、请求量、生成参数或服务端配置谁更好。
- 想确认模型不只是快，回答和工具调用也对。
- 想在给定延迟/吞吐要求下，找合适的负载点或容量方案。

如果只是临时手动调一下接口，通常不需要走完整评测流程。

## 主要功能

| 功能 | 说明 |
|---|---|
| 性能压测 | 对推理服务加压，测量延迟、吞吐、首包时间等 |
| 负载扫描 | 在多个并发、请求数或到达率上扫一遍，看性能怎么变化 |
| 参数扫描 | 组合不同服务端参数和压测参数，批量对比配置 |
| 正确性评测 | 检查回答对不对、工具调用行不行，不只看速度 |
| SLO 评测 | 按延迟和质量目标做搜索或仿真，辅助定容量和扩缩容 |

## 会产出什么

- 控制台可读的汇总结果
- 本地保存的配置、原始结果和指标，方便事后复查
- 可选的 Weights & Biases（W&B）实验记录与图表，方便跨 run 对比和选配置

## 示例

固定 prompt：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --prompt "hello" \
  --parallel 2 \
  --number 200
```

数据集文件：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset-path /home/wshiah/code/zhuting/foretoken/conversation.jsonl \
  --parallel 2 \
  --number 200
```
