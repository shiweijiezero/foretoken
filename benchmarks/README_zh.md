# 评测

[English](README.md) | 简体中文

使用 `foretoken bench` 测量 Foretoken 部署或现有 OpenAI 兼容端点的延迟和吞吐量。

## 开始前

从仓库根目录使用 Python 3.10 或更高版本运行评测命令：

```bash
pip install -e '.[bench]'
```

评测 Foretoken 部署时，先安装平台，再评测 Kustomize 配置：

```bash
foretoken install
foretoken bench examples/quickstart
```

快速开始服务已运行时，命令会直接复用；否则会部署渲染后的资源，并在评测结束后只删除本次创建的资源。

评测现有端点时，不需要 Foretoken 或 Kubernetes 平台：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --prompt "你好" \
  --parallel 2 \
  --number 20
```

## 结果与输出

不指定 `--output` 时，评测会打印汇总、在 `results/` 下保存本地产物，并尝试上传 W&B。W&B 不可用时，本地结果仍会保留。

`--output` 会替换默认输出选项：

| 目标 | `--output` 值 |
| --- | --- |
| 默认控制台、本地产物和 W&B | 不传 `--output` |
| 仅保存本地产物 | `local` |
| 保存本地产物但不输出控制台 | `local,quiet` |
| 保存本地产物并上传 W&B，但不输出控制台 | `local,wandb,quiet` |
| 仅上传 W&B | `wandb` |

如需关闭控制台输出但保留结果，请将 `quiet` 与 `local`、`wandb` 或两者组合。使用 `--output-dir PATH` 修改本地产物目录。

## 指标

汇总结果包括请求延迟、首个 token 时延（TTFT）、每输出 token 时延（TPOT）、失败率、输入/输出 token 数和输出吞吐量。

参数扫描中的 `token/s/user` 表示输出吞吐量除以配置的 closed-loop `--parallel` 值，不表示真实用户数或活跃会话数。open-loop（`--rate`）的分母固定为一，因此它等于总输出吞吐量。`token/s/GPU` 表示输出吞吐量除以该负载点配置的 GPU 数。

扫描会保存每个有效负载点；只有至少有两个有效负载点时，才会生成 `pareto/PARETO.png`。

## 下一步

数据集、随机提示词、轨迹回放、前缀复用、多数据集和参数扫描的配方见[评测示例](docs/examples_zh.md)。命令参数和结果格式可通过 `foretoken bench --help` 及本地产物查看。
