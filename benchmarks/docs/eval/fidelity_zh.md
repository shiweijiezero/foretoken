<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 比较量化保真度

[English](fidelity.md) | 简体中文 · [质量评测](README_zh.md)

`foretoken eval --evaluator fidelity` 用于比较量化前后，模型预测下一个 token 的概率发生了多大变化。所有模型都接收同一段语料的 token ID；后续上下文继续使用原文，不拼入模型生成的答案。这种比较方式称为 teacher forcing。

## 比较一个候选模型

参考模型和候选模型需要使用相同的 tokenizer。`--tokenizer-path` 指定共同基础模型的仓库或本地目录，其中包含 tokenizer 与 `config.json`。两端服务应通过 `/v1/completions` 返回以 token ID 为键的完整词表对数概率。使用 vLLM 时，启动两端服务都加上 `--max-logprobs -1`。使用 Foretoken 部署时，在模型的 `spec.engineArgs` 中加入 `max-logprobs: -1`，再用 `foretoken deploy PATH` 应用配置。

下面假设 BF16 参考服务位于 8009 端口、bitsandbytes 4-bit 候选服务位于 8008 端口，两端都提供 `Qwen/Qwen3-0.6B`。请换成实际服务地址和模型 ID：

```bash
foretoken eval --evaluator fidelity \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --reference-url http://127.0.0.1:8009/v1/chat/completions \
  --reference-model Qwen/Qwen3-0.6B \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --label "bitsandbytes 4-bit" --method bitsandbytes --weight-bits 4 \
  --output local,wandb
```

终端会汇总 KL 散度、Top-1 一致率和去均值 logit 的均方根误差。结果目录保存对比图及逐位置指标，W&B 的 Fidelity 分区也会展示这些结果。只需要本地结果时，使用 `--output local`。

也可以在 `eval` 后紧接一个 Kustomize 目录，替代候选服务的 `--url`；参考部署用 `--reference PATH` 指定。单模型部署会自动提供模型 ID。如果两个模型共用一个服务地址，只需用 `--reference-model` 选择参考模型，无需重复填写 `--reference-url`。

## 比较多种方法与位宽

将候选列表保存为 `candidates.jsonl`，每行一个模型。可以复用命令中的服务地址，只覆盖 `model`，也可以逐行指定 `url` 或 Kustomize `path`：

```jsonl
{"model":"qwen-bf16","label":"BF16","method":"BF16","weight_bits":16}
{"model":"qwen-bnb4","label":"bitsandbytes 4-bit","method":"bitsandbytes","weight_bits":4}
```

模型 ID 应填写服务实际提供的名称。将前面命令中的单候选选项 `--label`、`--method` 和 `--weight-bits` 换成 `--candidates candidates.jsonl` 即可。每个候选的 `label` 必须不同。

图表按方法着色，并标注候选名称。位宽和模型大小分别作为横轴绘图：

| 候选字段 | 含义 |
| --- | --- |
| `weight_bits` | 名义权重精度，例如 4 位或 16 位 |
| `bits_per_weight` | 包含量化开销在内，实测每个权重占用的有效位数 |
| `model_size_gib` | 实测模型 checkpoint 大小，单位 GiB |

有实测值时再填写有效位宽和大小；没有大小信息时，横轴使用候选名称。单候选命令对应使用 `--weight-bits`、`--bits-per-weight` 和 `--model-size-gib`。

## 选择语料和评分位置

默认使用 [WikiText-2](https://huggingface.co/datasets/Salesforce/wikitext) 的 `wikitext-2-raw-v1` 配置、test 划分。按顺序取 4 个互不重叠的 512-token 窗口，对每个窗口最后 16 个位置评分，每个候选共比较 64 个位置。

本地文本使用 `--dataset corpus.txt`；包含 `text` 字段的 JSONL 使用 `--dataset corpus.jsonl`，字段名称可通过 `--text-column` 修改。Hugging Face 数据集还可用 `--dataset-config` 和 `--split` 选择配置与划分。

通过 `--context-length`、`--num-windows` 和 `--score-tokens` 调整比较规模。tokenizer 定义了句首 token 时，每个窗口会添加该 token。参考模型的概率只采集一次，供本次运行的所有候选复用。

## 理解结果

| 指标 | 如何解读 |
| --- | --- |
| 平均、中位数、p99 KL | 在完整词表上计算 `KL(reference || candidate)`，单位 nats，越低越接近参考模型 |
| Top-1 一致率 | 概率最高的 token 相同的位置比例 |
| Top-k 重叠率 | 两组前 k 个 token 的交集大小除以 k；默认 k=5、10，可用 `--top-k` 调整 |
| 原文 token 概率变化 | 候选模型对原文 token 的概率减去参考模型概率；平均值表示方向，均方根表示幅度 |
| Centered-logit RMSE | 分别减去各自的平均对数概率，再计算均方根误差；整体 logit 平移不影响该指标 |
| Total variation | 两个概率向量逐项差值的绝对值之和的一半 |

位宽和大小对比图展示平均 KL、p99 KL、Centered-logit RMSE 与 Top-1 一致率。逐位置曲线帮助定位哪些文本位置的概率分布差异更大。答案正确率使用[任务质量评测](README_zh.md)，服务速度使用[性能评测](../perf/README_zh.md)。

下面展示通过已有服务比较 Qwen3-0.6B BF16 与 bitsandbytes 4-bit 的结果：语料取两个 96-token WikiText-2 窗口，每个窗口比较最后 32 个位置。

![按量化方法和名义位宽比较 KL、logit 均方根误差及 Top-1 一致率](../imgs/fidelity-weight-bits.png)

![64 个评分位置的 KL 与去均值 logit 差异](../imgs/fidelity-positions.png)

结果目录中的 `fidelity_candidates.csv` 保存候选汇总，`fidelity_tokens.jsonl` 保存逐位置指标，PNG 文件可直接查看。`metrics.json` 记录语料选择、评分设置和完成情况。完整概率向量仅临时保存，比较结束后删除。
