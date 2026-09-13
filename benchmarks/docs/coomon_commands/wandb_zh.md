# W&B 输出

[English](wandb.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，指定项目、分组和运行名称：

```bash
wandb login

foretoken bench examples/quickstart \
  --number 20 --output local,wandb \
  --wandb-project foretoken-bench \
  --wandb-group qwen-comparison \
  --wandb-run-name quickstart
```

`--wandb-entity` 选择账号或团队。group 和运行名分别设置。扫描、多数据集在未指定 group 时自动分组，各子运行会在名称后追加标识。单次评测默认不分组。

仅本地输出用 `--output local`，不打印汇总用 `--output local,quiet`，仅上传用 `--output wandb`。
