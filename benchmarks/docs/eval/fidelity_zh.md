<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 比较模型概率分布

[English](fidelity.md) | 简体中文 · [质量评测](README_zh.md)

给 `foretoken eval` 添加 `--reference`，即可比较候选模型与参考模型预测下一个 token 的概率差异，报告完整词表 KL、Top-1/Top-k 一致率和 logit 差异，并生成本地图表及 W&B 结果。
## 比较量化模型

按[量化模型示例](../../../examples/quantized-model/README_zh.md)准备源码安装的平台及模型存储后，在仓库根目录运行：

```bash
foretoken eval examples/quantized-model/bitsandbytes \
  --reference examples/quantized-model/bf16 \
  --output local
```

两端使用相同的 Qwen2.5-0.5B-Instruct 和 BF16 计算精度，候选模型以 4-bit 加载权重。命令直接读取参考部署中的模型与 tokenizer。已有部署会被复用；不存在的部署会临时创建，并在使用后删除。参考模型先于候选模型运行，因此两端都为临时部署时，一张可用 GPU 即可依次完成比较。

示例已允许返回完整词表概率。如果之前用旧版示例创建了部署，先用 `foretoken deploy PATH` 应用更新后的配置。自定义部署需在模型的 `spec.engineArgs` 中设置 `max-logprobs: -1`；原生 vLLM 服务使用 `--max-logprobs -1`。

## 一次比较多个候选

维护中的候选列表包含 BF16 和 bitsandbytes 两项，已标注方法与名义位宽：

```bash
foretoken eval \
  --reference examples/quantized-model/bf16 \
  --candidates examples/quantized-model/candidates.jsonl \
  --output local,wandb
```

自定义列表每行写一个 JSON 对象：

```jsonl
{"path":"examples/quantized-model/bf16","label":"BF16","method":"BF16","weight_bits":16}
{"path":"examples/quantized-model/bitsandbytes","label":"4-bit","method":"bitsandbytes","weight_bits":4}
```

相对路径以命令的工作目录为基准。每行可指定部署 `path`，或者服务 `url` 及其 `model`；两者都省略时复用命令中的候选服务。单模型部署会自动提供模型 ID。显示名称默认取部署目录名或模型 ID；名称相同时用 `label` 区分。

单候选命令中的 `--label`、`--method`、`--weight-bits` 都是可选的绘图标注。未指定方法时，部署对比会读取 `engineArgs` 中的量化方法。有效位宽和 checkpoint 大小是可选实测坐标，不是运行前提：

| 候选字段 / 命令选项 | 含义 |
| --- | --- |
| `weight_bits` / `--weight-bits` | 名义权重精度 |
| `bits_per_weight` / `--bits-per-weight` | 包含量化开销在内，实测每个权重占用的位数 |
| `model_size_gib` / `--model-size-gib` | 实测 checkpoint 大小，单位 GiB |

提供哪种坐标，就生成相应的对比图；没有大小信息时，横轴使用候选名称。

## 比较已有服务

分别指定候选与参考服务的地址和模型 ID：

```bash
foretoken eval \
  --url http://127.0.0.1:8008/v1/chat/completions --model quantized \
  --reference-url http://127.0.0.1:8009/v1/chat/completions \
  --reference-model Qwen/Qwen3-0.6B \
  --output local
```

将地址和模型 ID 换成实际服务。命令默认用参考模型 ID 获取 tokenizer 和模型配置；如果该 ID 只是服务别名，或者模型文件仅在集群节点上可见，用 `--tokenizer-path` 指定基础模型仓库，或客户端本地包含 tokenizer 文件与 `config.json` 的目录。Foretoken 部署则自动按配置的 Hugging Face、ModelScope 或客户端可访问的本地来源解析，也支持部署中单独指定的 tokenizer。

两端需要使用相同的 token ID 映射与模型词表。若共用服务地址，可以省略 `--reference-url`。认证使用 `--api-key`；参考服务凭据不同则使用 `--reference-api-key`。

## 选择语料和评分位置

默认使用 [WikiText-2](https://huggingface.co/datasets/Salesforce/wikitext) 的 `wikitext-2-raw-v1` 配置、test 划分，取 4 个互不重叠的 512-token 窗口，分别比较最后 16 个位置，每个候选共 64 个位置。上下文始终来自原文，不拼入模型生成答案，这种方式称为 teacher forcing。

本地文本使用 `--dataset corpus.txt`；包含 `text` 字段的 JSONL 使用 `--dataset corpus.jsonl`，字段名称可用 `--text-column` 修改。Hugging Face 数据集还可用 `--dataset-config`、`--split` 选择配置和划分。通过 `--context-length`、`--num-windows`、`--score-tokens` 调整比较规模。tokenizer 定义了句首 token 时，每个窗口会添加该 token。

## 恢复模型对比

在原比较命令中追加 `--resume`，指向该次运行的结果目录。将下面的 `results/previous-run` 换成中断时打印的实际目录：

```bash
foretoken eval examples/quantized-model/bitsandbytes \
  --reference examples/quantized-model/bf16 \
  --resume results/previous-run --output local
```

已完成的窗口直接复用，中断的窗口整体重算；语料使用原来保存的 token。参考或候选模型已全部算完时，无需再次部署或发送请求。保持模型及其权重、tokenizer、候选列表和评分设置不变。恢复结果写入新的目录，原运行保持不变。

## 理解结果

| 指标 | 如何解读 |
| --- | --- |
| 平均、中位数、p99 KL | 在完整词表上计算 `KL(reference || candidate)`，单位 nats，越低越接近参考模型 |
| Top-1 一致率 | 概率最高的 token 相同的位置比例 |
| Top-k 重叠率 | 两组前 k 个 token 的交集大小除以 k；默认 k=5、10，可用 `--top-k` 调整 |
| 原文 token 概率变化 | 候选模型对原文 token 的概率减去参考模型概率；平均值表示方向，均方根表示幅度 |
| Centered-logit RMSE | 分别减去各自的平均对数概率，再计算均方根误差；整体 logit 平移不影响该指标 |
| Total variation | 两个概率向量逐项差值的绝对值之和的一半 |

下图通过已有服务比较 Qwen3-0.6B BF16 与 bitsandbytes 4-bit：取两个 96-token WikiText-2 窗口，每个窗口比较最后 32 个位置。

![按名义位宽比较 KL、logit 均方根误差及 Top-1 一致率](../imgs/fidelity-weight-bits.png)

![64 个评分位置的 KL 与去均值 logit 差异](../imgs/fidelity-positions.png)

命令打印的结果目录包含 `fidelity_candidates.csv`、`fidelity_tokens.jsonl` 和 PNG 图表；`metrics.json` 记录评分协议与完成情况。给 `--output` 加上 `wandb` 即可发布表格、曲线和图片。需要续跑时保留完整的本地结果目录，其中保存了采样 token、参考概率和评分进度。答案正确率使用[任务质量评测](README_zh.md)，服务速度使用[性能评测](../perf/README_zh.md)。
