# 请求速率与并发

[English](arrival-rate.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，按平均每秒 5 个请求发送，同时最多允许 16 个请求在途：

```bash
foretoken bench examples/quickstart \
  --prompt "你好" --rate 5 --parallel 16 --number 100 \
  --output local,wandb
```

`--rate` 控制泊松到达率，`--parallel` 控制并发，各自设为 `-1` 表示不限。默认不限速、并发为 1。

去掉并发上限：

```bash
foretoken bench examples/quickstart \
  --prompt "你好" --rate 5 --parallel -1 --number 100 \
  --output local,wandb
```

`--rate -1 --parallel -1` 会尽快启动指定数量的全部请求。多轮数据目前要求 `--rate -1`，此时并发统计对话数。

## 输出示例

以下为较低到达率的小规模运行：

![命令行输出](../imgs/arrival-rate-cli.png)

![W&B 运行页面](../imgs/arrival-rate-wandb.png)
