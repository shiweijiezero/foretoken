<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 比较参考模型与候选模型

[English](distribution-comparison.md) | 简体中文 · [质量评测](README_zh.md)

给 `foretoken eval` 添加 `--reference`，即可比较候选模型与参考模型。默认使用相同的原文前缀，比较下一个 token 的完整词表 KL、Top-1/Top-k 一致率和 logit 差异；要比较实际生成的 token，使用[贪心生成序列对比](#比较贪心生成序列)。

## 比较量化模型

按[量化模型示例](../../../examples/quantized-model/README_zh.md)准备源码安装的平台及模型存储后，在仓库根目录运行：

```bash
foretoken eval examples/quantized-model/bitsandbytes \
  --reference examples/quantized-model/bf16 \
  --output local,plot
```

两端使用相同的 Qwen2.5-0.5B-Instruct 和 BF16 计算精度，候选模型以 4-bit 加载权重。模型与 tokenizer 设置从参考部署读取。已有部署直接复用；临时部署依次运行、用完删除，因此两端都为临时部署时，一张可用 GPU 即可完成比较。

## 理解结果

汇总表通过以下指标比较候选模型：

| 指标 | 如何解读 |
| --- | --- |
| 平均、中位数、p99 KL | 在完整词表上计算 `KL(reference || candidate)`，单位 nats，越低越接近参考模型 |
| Top-1 一致率 | 概率最高的 token 相同的位置比例 |
| Top-k 重叠率 | 两组前 k 个 token 的交集大小除以 k；默认 k=5、10，可用 `--top-k` 调整 |
| 原文 token 概率变化 | 候选模型对原文 token 的概率减去参考模型概率；平均值表示方向，均方根表示幅度 |
| Centered-logit RMSE | 分别减去各自的平均对数概率，再计算均方根误差；整体 logit 平移不影响该指标 |
| Total variation | 两个概率向量逐项差值的绝对值之和的一半 |

在结果目录中，`distribution_comparison_candidates.csv` 汇总各候选的指标，`distribution_comparison_positions.jsonl` 用于查看逐位置差异，`plots/` 保存对比图。

下图通过已有服务比较 Qwen3-0.6B BF16 与 bitsandbytes 4-bit：取两个 96-token WikiText-2 窗口，每个窗口比较最后 32 个位置。

![按名义位宽比较 KL、logit 均方根误差及 Top-1 一致率](../imgs/distribution-comparison-weight-bits.png)

![64 个评分位置的 KL 与去均值 logit 差异](../imgs/distribution-comparison-positions.png)

答案正确率使用[任务质量评测](README_zh.md)，服务速度使用[性能评测](../perf/README_zh.md)。

## 选择语料和评分位置

默认使用 [WikiText-2](https://huggingface.co/datasets/Salesforce/wikitext) 的 `wikitext-2-raw-v1` 配置、test 划分，取 4 个互不重叠的 512-token 窗口，分别比较最后 16 个位置，每个候选共 64 个位置。上下文始终来自原文，不拼入模型生成答案，这种方式称为 teacher forcing。

本地文本使用 `--dataset corpus.txt`；包含 `text` 字段的 JSONL 使用 `--dataset corpus.jsonl`，字段名称可用 `--text-column` 修改。Hugging Face 数据集还可用 `--dataset-config`、`--split` 选择配置和划分。通过 `--context-length`、`--num-windows`、`--score-tokens` 调整比较规模。

## 一次比较多个候选

常用场景直接把候选 Kustomize 部署目录写在选项前：

```bash
foretoken eval examples/quantized-model/bitsandbytes examples/quantized-model/bf16 \
  --reference examples/quantized-model/bf16 --output local,wandb,plot
```

每个候选分别与参考模型比较。需要自定义名称或模型大小信息时，可以使用[候选配置文件](../../../examples/quantized-model/candidates.jsonl)：

```bash
foretoken eval \
  --reference examples/quantized-model/bf16 \
  --candidates examples/quantized-model/candidates.jsonl \
  --output local,wandb,plot
```

自定义列表可参照该文件，每行用一个 JSON 对象描述候选。每行指定部署 `path`，或服务 `url` 及其 `model`；两者都省略时复用命令中的候选服务。相对路径以命令的工作目录为基准。单模型部署会自动提供模型 ID，显示名称默认取部署目录名或模型 ID，名称相同时用 `label` 区分。

单候选命令可用 `--label`、`--method` 标注图表。未指定方法时，自动采用部署的量化方式。以下大小信息都是可选的：提供哪种坐标，就生成相应的对比图；没有大小信息时，横轴使用候选名称。

| 候选字段 / 命令选项 | 含义 |
| --- | --- |
| `weight_bits` / `--weight-bits` | 名义权重精度 |
| `bits_per_weight` / `--bits-per-weight` | 包含量化开销在内，实测每个权重占用的位数 |
| `model_size_gib` / `--model-size-gib` | 实测 checkpoint 大小，单位 GiB |

## 比较已有服务

分别提供两端的服务地址和模型 ID，将下面的示例值换成实际服务。两端需要使用相同的 token ID 映射与模型词表，并支持完整词表概率输出。原生 vLLM 使用 `--max-logprobs -1`；自定义 Foretoken 部署则在模型的 `spec.engineArgs` 中设置 `max-logprobs: -1`。

```bash
foretoken eval \
  --url http://127.0.0.1:8008/v1/chat/completions --model quantized \
  --reference-url http://127.0.0.1:8009/v1/chat/completions \
  --reference-model Qwen/Qwen2.5-0.5B-Instruct \
  --output local,plot
```

命令默认用参考模型 ID 获取 tokenizer 和模型配置。如果该 ID 只是服务别名，或者模型文件仅在集群节点上可见，用 `--tokenizer-path` 指定基础模型仓库，或客户端本地包含 tokenizer 文件与 `config.json` 的目录。Foretoken 部署会自动按配置的 Hugging Face、ModelScope 或客户端可访问的本地来源解析，也支持单独指定的 tokenizer。

若共用服务地址，可以省略 `--reference-url`。认证使用 `--api-key`；参考服务凭据不同则使用 `--reference-api-key`。

## 比较贪心生成序列

使用维护中的候选列表，在相同输入下比较参考模型和候选模型实际生成的 token ID：

```bash
foretoken eval --reference examples/quantized-model/bf16 \
  --candidates examples/quantized-model/candidates.jsonl \
  --greedy-compare --context-length 128 --num-windows 4 --max-tokens 64 \
  --output local,wandb,plot
```

命令用四段 128-token 输入比较贪心生成结果（`temperature=0`），每段最多生成 64 个 token，遇到 EOS 时提前结束。两端需采用相同的 token ID 映射，并在 Completions 响应中返回 `choices[0].token_ids` 和 `finish_reason`。模型的上下文长度应容纳输入及生成预算。

`greedy_comparison_candidates.csv` 汇总有效、失败样本数，以及有效样本中的完整序列一致率。需要定位差异时，查看 `greedy_comparison_samples.csv` 中的生成 token 和首个分歧位置。

比较使用同一服务模型名的不同 draft 配置时，为每个候选指定不同的部署路径或 URL，并用 `label` 区分。

## 恢复概率分布对比

保留概率分布对比的完整本地结果目录。中断后，在原命令中追加 `--resume`，指向该目录。将下面的 `results/previous-run` 换成运行时打印的实际路径：

```bash
foretoken eval examples/quantized-model/bitsandbytes \
  --reference examples/quantized-model/bf16 \
  --resume results/previous-run --output local,plot
```

恢复时复用已完成的工作，结果写入新目录。模型及其权重、tokenizer、候选列表和评分设置应保持不变。
