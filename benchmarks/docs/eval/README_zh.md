<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 评测模型质量

[English](README.md) | 简体中文 · [评测与性能剖析](../../README_zh.md)

使用 lm-evaluation-harness 或 EvalScope 评测生成文本，使用 VBench 评测生成视频。完成[准备步骤](../../README_zh.md#开始使用)后，选择下面的评测器运行。添加 `--reference` 可[比较参考与候选模型的概率分布](distribution-comparison_zh.md)，查看 KL、位宽对比图和 logit 差异。

## lm-evaluation-harness

先评测 100 道 GSM8K 数学题：

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval \
  --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 \
  --output local,wandb
```

汇总结果列出任务得分、答案提取方式、样本数，以及框架提供的标准误差。默认框架是 `lm-eval`，任务名称和参数直接采用[上游 CLI 的写法](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/interface.md)：

- `--num_fewshot 0` 使用零样本提示。
- `--log_samples` 保存逐题输入和回答。
- `--model_args num_concurrent=4` 同时发送四个 API 请求。

### 候选答案似然与困惑度

PIQA 通过比较候选答案的概率选择答案。WikiText 测量文本困惑度（PPL），数值越低，表示原文越容易被模型预测。这两类任务需要[源码安装的 Foretoken 平台](../../../docs/custom-deployment_zh.md)，或能返回输入 token 对数概率的已有 Completions 服务：

```bash
foretoken eval examples/quickstart \
  --tasks piqa --limit 100 --output local

foretoken eval examples/quickstart \
  --tasks wikitext --limit 100 --output local
```

分词器从部署配置读取；使用已有 URL 时默认采用 `--model`。若模型名是服务别名，或文件仅在服务器可见，可用 `--model_args tokenizer=MODEL_OR_LOCAL_DIRECTORY` 指定实际模型仓库或客户端本地分词器目录。

候选答案评分默认使用原始文本，任务要求指令模型模板时添加 `--apply_chat_template`。困惑度评测使用原始语料，不套用聊天模板。

## EvalScope

```bash
foretoken eval examples/quickstart \
  --evaluator evalscope \
  --model Qwen/Qwen3-0.6B \
  --datasets gsm8k --limit 100 \
  --output local,wandb
```

汇总结果展示任务得分和已完成评分的样本数，各类别和子集的详细分数保存在报告与 W&B 中。通过 [EvalScope 原生参数](https://evalscope.readthedocs.io/zh-cn/latest/get_started/basic_usage.html)配置任务，例如 `--dataset-args` 和 `--generation-config`。

两个框架都可去掉 `--limit`，运行完整的所选任务。提示词和判分规则由框架及任务定义。全部选项分别见 `foretoken eval --evaluator lm-eval --help` 和 `foretoken eval --evaluator evalscope --help`。

## VBench custom_input 模式

自动安装适用于 Linux x86_64、支持 CUDA 12.1 的 NVIDIA 驱动。先激活 Conda，准备好 `git`、`wget` 和 `unzip`，然后在项目目录执行一次初始化，即可评测已有 MP4 或 GIF 视频：

```bash
foretoken eval setup vbench
foretoken eval --video results/video-run \
  --evaluator vbench \
  --output local,wandb
```

setup 在 YAML 所在目录的 `.foretoken/evaluators/vbench` 下创建独立 Python 3.10 Conda 环境、经过验证的 VBench 源码和权重缓存，使用 CUDA 12.1 的 PyTorch 2.5.1 / torchvision 0.20.1，准备全部 10 个 custom_input 维度，但不运行 GPU 评测。它不会修改 Foretoken 的 Python 依赖。安装和下载需要联网及数 GB 磁盘空间；上游下载涉及 Hugging Face 等多个站点，不能仅靠 HF 镜像覆盖所有来源。

准备成功后，setup 自动写入 `foretoken-evaluators.yaml`，之后评测直接复用，不安装依赖。两个命令都会从当前目录向上查找最近的配置；找不到时，setup 在当前目录生成文件。通过 `--config PATH` 指定其他 YAML，setup 的 `--directory PATH` 可以调整托管安装位置。失败时不会发布新的 YAML，保留安装文件，解决报错后重试即可。权重准备日志保存在托管目录的 `setup.log`。

`foretoken perf video` 生成视频并测量服务性能，`foretoken eval --video` 只评测已有视频的质量，不会重新生成。如果视频目录是 Foretoken `perf video` 的结果目录，命令会自动从 `raw_results.json` 读取每个视频的准确提示词。对于其他视频目录，VBench 会从文件名推断提示词，也可以通过 `--prompt-file` 传入 VBench JSON 映射。使用 `--dimension NAME [NAME ...]` 选择部分维度；默认运行 `custom_input` 支持的全部 10 个维度。

新评测结果的 `config.json` 会记录 VBench Python、源码目录、Git commit、所选维度、提示词来源和视频数量。VBench 目录不是 Git 检出时，`vbench_commit` 为 `null`。Foretoken 视频生成阶段已在源目录保存自己的 `config.json` 和 `raw_results.json`。

### 使用自行安装的环境

如果已经安装 VBench，或自动安装不适合当前平台、网络，可以按[上游安装说明](https://github.com/Vchitect/VBench#installation)自行安装，手写下面的 YAML，将示例路径替换为实际路径：

```yaml
evaluators:
  vbench:
    python: /path/to/vbench-env/bin/python
    root: /path/to/VBench
    cache: /path/to/vbench-cache
```

随后直接运行 `foretoken eval --video VIDEO_DIR`，无需 setup。已有这份 YAML 时运行 setup，只检查指定的安装并准备缺失权重，不重新安装依赖，也不改写 VBench 配置。已有配置有误时需要自行修正。setup 新增 VBench 配置时会保留其他评测器的设置。

VBench 使用配置的独立 Python 环境运行；`root` 包含 `evaluate.py`，`cache` 是已存在的权重目录。`cache` 可以省略，此时使用 VBench 默认缓存。YAML 中的相对路径以配置文件所在目录为基准。`--vbench-python`、`--vbench-root` 和 `--vbench-cache` 分别覆盖 YAML 中的路径；评测过程中，VBench 仍可能下载缺少的权重。

## 评测已有服务

将部署目录换成服务的 Chat Completions URL，并指定模型名：

```bash
foretoken eval \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --evaluator lm-eval \
  --model Qwen/Qwen3-0.6B \
  --tasks gsm8k --limit 100 \
  --output local,wandb
```

此模式不使用 Kubernetes 资源。需要认证时添加 `--api-key`。Foretoken Gateway 部署则传入 Kustomize 目录，由命令查找地址并配置路由请求头。

## 恢复中断的评测

保留本地输出即可保存进度。中断后，在原命令中追加 `--resume`，指向该次运行打印的结果目录。将下面的 `results/previous-run` 换成实际目录：

```bash
foretoken eval examples/quickstart \
  --evaluator lm-eval --tasks gsm8k --limit 100 \
  --resume results/previous-run --output local
```

恢复会创建新的结果目录，复用已完成工作，并汇总完整评分；原目录保持不变。如果再次中断，从最新目录继续恢复。模型权重、任务配置、生成参数和样本范围应保持不变。

| 评测类型 | 复用的工作 |
| --- | --- |
| lm-evaluation-harness | 已完成的文本生成结果（包括多次采样），以及候选答案和困惑度任务已完成的似然评分窗口 |
| EvalScope | 服务地址和评测设置不变时，复用独立样本已完成的预测和评分 |
| 模型概率分布对比 | 已完成的评分窗口，见[恢复模型对比](distribution-comparison_zh.md#恢复模型对比) |

按上述方式恢复时，使用 `--resume`，不再指定原生 `--use_cache` 或 `--use-cache`。

## 查看评分

打开命令打印的结果目录：

| 文件或目录 | 内容 |
| --- | --- |
| `metrics.json` | 任务得分、子集、答案提取方式、样本数，以及框架提供的不确定性或执行状态 |
| `native/` | 框架报告及其生成的逐样本记录 |
| `evaluator.log` | 评测框架的运行日志 |

W&B 提供任务指标、分数表，并将框架报告作为 artifact 供下载。输出位置与运行分组采用通用[结果设置](../../README_zh.md#查看和保存结果)。

比较分数时，使用相同的框架、任务配置和样本范围。
