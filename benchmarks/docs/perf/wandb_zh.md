# W&B 输出

[English](wandb.md) | 简体中文 · [性能评测示例](README_zh.md)

完成[准备步骤](README_zh.md#准备)后，指定项目、分组和运行名称：

```bash
foretoken perf examples/quickstart/qwen3-0.6b \
  --num-prompts 20 --output local,wandb \
  --wandb-project foretoken-bench \
  --wandb-group qwen-comparison \
  --wandb-run-name quickstart
```

打开终端打印的运行链接即可查看结果，quiet 模式也会显示链接。`--wandb-entity` 选择账号或团队。需要对比的运行使用相同的 `--wandb-group`，在该分组的 Workspace 中查看。扫描和多数据集运行在未指定 group 时自动分组，单次评测默认不分组。

## 查看进度与比较结果

用逐请求曲线检查单次请求，用累计曲线观察整体进度，用一秒完成窗口比较吞吐量及 P50/P95/P99 延迟随时间的变化。HTTP 正式测量期间会持续更新这些视图；窗口可能需要等待较早发出的请求结束。测量结束后展示最终汇总指标，Summary 保留最终值。

![逐请求耗时与 token 数](../imgs/request-order-wandb.png)

混合负载可按数据集、模型和请求类别查看细分结果。Kustomize 评测还提供副本数和 GPU 分配量观测；服务具备 Prometheus 时，可查看资源使用和猜测解码指标。定义、单位和观测覆盖范围见[性能指标](../metrics_zh.md)。

## 下载记录

在运行页面的 Artifacts 中下载配置、指标、请求记录和日志。扫描或对比任务的共享服务准备、清理日志保存在父运行中。各类文件的用途和输出选项见[结果设置](../../README_zh.md#查看和保存结果)。

预热不计入性能指标，也不占用面板，记录保存在 `benchmark-warmup` 附件中。在 `--output local,wandb` 中加上 `plot`，即可将导出的图表保存在本地和 `benchmark-plots` 附件中。

## 恢复上传

评测时设置 `WANDB_MODE=offline`，即可只在本地记录、不尝试上传。之后需要补传，或后台上传失败时，将终端打印的结果目录传给：

```bash
foretoken plot RESULT_DIR --sync_wandb
```

命令重绘本地图表，并将已有指标、表格、媒体和日志同步到各自原来的 W&B 运行，不请求模型。中断运行也可以上传已有记录和诊断。绘图失败会报告错误，但仍尝试同步结果；绘图或同步任一阶段失败，命令都会以失败状态退出。

单次运行的图表会随该运行上传，使用 `--output-dir` 时也一样；跨运行的对比图保留在本地。重复执行会更新归属明确的图表，不重复发布已上传的测量历史。

需要换一台机器上传时，在目标机器安装 Foretoken、执行 `wandb login`，并复制完整结果目录树，包括隐藏文件。结果可以放在不同的绝对路径下；若续跑实验引用了此前的任务目录，也需一并复制并保留它们之间的相对位置。自托管 W&B 使用 `WANDB_BASE_URL` 指定原服务器。

不加 `--sync_wandb` 时，`foretoken plot` 只导出本地文件。继续未完成的测量仍使用 `perf --resume`。较旧版本记录若引用了已丢失的外部 W&B 文件，仍需先恢复那些文件。
