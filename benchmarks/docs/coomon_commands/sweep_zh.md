# 参数扫描

[English](sweep.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，使用现有[参数文件](../../examples/sweep.jsonl)比较不同并发下的表现：

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 256 --random-seed 0 \
  --temperature 0 \
  --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 \
  --output local,wandb
```

参数文件在 1、2、4 并发下各发送 384 个请求，每个请求要求输出 256 个 token。`--num-runs 3` 将每个参数点重复三次；`--warmup-requests 16` 在每次重复前完成独立预热，默认不预热。

每行 JSONL 定义一组参数，`parallel`、`number` 或 `rate` 的列表会展开成负载点。同一行只能将 `parallel` 或 `rate` 中的一个设为多值列表；`number` 也是多值列表时，长度需与该轴一致。

扫描行可以覆盖负载、生成和数据集设置，包括 `warmup_requests`。部署、凭据和结果去向保持不变。参数扫描要求使用 Kustomize 部署，不与 `--url`、轨迹回放或多数据集组合。

## 查看结果

命令会打印 `results/` 下的结果目录。`sweep_points.json` 保留每次重复；`sweep_summary.json` 和 `sweep_summary.csv` 按参数点汇总均值、中位数、样本标准差及范围。先检查成功请求数，再比较各次运行的时延和吞吐量。单位与缺失值的含义见[实验记录](../../metrics_zh.md#实验记录)。

每次重复都有独立的请求记录和 `warmup/` 目录，预热结果不计入正式指标。有效点足够时，`pareto/PARETO.png` 比较每个配置用户与每张声明 GPU 的输出 token 吞吐量。

可以用 `--experiment-name baseline` 为实验命名，省略时自动创建目录。每次实验使用不同名称。

## 对比推理配置

比较计算精度、量化或推测解码时，通过[推理参数](../../../docs/inference-parameters_zh.md)配置各方案，保持硬件、副本数、tokenizer、负载及其他引擎设置一致。使用同一模型的对应 checkpoint；比较 AWQ 权重量化时，普通精度基线使用相同且受支持的计算精度。

各方案使用相同的前缀缓存策略和预热预算。若要排除重复前缀复用的影响，设置 `spec.engineArgs.enable-prefix-caching: false`。自动预热复用测量的起始数据行和随机种子；需要互斥数据时，通过独立命令选择不同的数据偏移。

每种配置先部署，再执行扫描：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

在前面的评测命令中添加 `--experiment-name baseline --wandb-group comparison`，后续方案改用 `awq`、`ngram` 等不同实验名，保持同一分组。修改模型配置后重新执行 `foretoken deploy`：`bench` 复用已有服务时不会应用 YAML 修改。

对于名为 `baseline` 的实验，在结果旁保存部署配置：

```bash
kubectl kustomize examples/quickstart > results/baseline/deployment.yaml
git rev-parse HEAD > results/baseline/commit.txt
```

每次重复的 `environment.json` 会记录客户端和观察到的服务设置。服务器 GPU、驱动、推理引擎版本、解析后的模型与 tokenizer 修订版本，以及复现实验所需的本地配置改动，也应随结果保留。

在 W&B 分组或本地汇总中比较相同负载点，同时保留成功和失败的重复结果。随机输入用于控制长度；评估真实任务性能时，使用固定且有代表性的[对话数据集](conversations_zh.md)，移除扫描行中的 `min_output_length` 和 `max_output_length`，并设置统一的 `max_tokens` 上限。量化还需评估输出质量。

需要分析耗时时，单独执行[性能剖析](../../../observability/profiling_zh.md)。完成对比后，执行 `foretoken delete examples/quickstart` 清理显式部署的服务。

## 输出示例

单张 A100 80GB PCIe 上的 Qwen3-0.6B：

![扫描命令的实际输出](../imgs/sweep-cli.png)

W&B 按一秒完成窗口展示 E2EL p95；帕累托图比较整组吞吐量。

![按一秒完成窗口统计的 E2EL p95 时间曲线](../imgs/sweep-wandb.png)

![实测帕累托前沿](../imgs/sweep-pareto.png)
