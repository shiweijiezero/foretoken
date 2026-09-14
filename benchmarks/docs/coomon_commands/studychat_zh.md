# StudyChat 轨迹回放

[English](studychat.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，回放记录中的到达时间和请求内容：

```bash
foretoken bench examples/quickstart \
  --trace KrisQ/StudyChat --dataset KrisQ/StudyChat \
  --trace-start 600 --trace-duration 300 \
  --trace-max-concurrency 32 --output local,wandb
```

`--trace` 提供到达时间，`--dataset` 提供内容。选择同一来源时直接使用各记录的消息。从轨迹开始后的第 600 秒起回放，持续 300 秒；并发等待计入回放延迟。

每条记录独立执行，请求数量和到达时间由窗口决定，不添加 `--number`、`--rate`、`--parallel` 或正数 `--max-turns`。在途请求数由 `--trace-max-concurrency` 控制。

## 输出示例

以下为采用 StudyChat 格式的本地短轨迹，不是完整远程数据集：

![命令行输出](../imgs/trace-studychat-benchmark-output.png)

![W&B 运行页面](../imgs/trace-studychat-wandb-dashboard.png)
