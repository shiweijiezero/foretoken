# 参数扫描

[English](sweep.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，复用现有[参数文件](../../examples/sweep.jsonl)做重复对比。它在 1、2、4 并发下各发送 384 个请求，每个请求要求输出 256 个 token。下面的命令将每个点重复三次。

使用固定副本数和 GPU 配额的诊断部署。比较推理设置时，各方案均设置 `spec.engineArgs.enable-prefix-caching: false`，避免重复输入让实验变成前缀缓存测试。随结果记录模型与 tokenizer 修订版本、GPU 和驱动、运行镜像摘要、vLLM 版本及引擎设置。

先为这次实验命名并部署基线：

```bash
EXPERIMENT="quickstart-$(date -u +%Y%m%dT%H%M%SZ)"
VARIANT=baseline
foretoken deploy examples/quickstart --timeout 20m
```

保留此部署，在每次重复测量前预热。16 段对话只是初始预算，若时延尚未稳定，应对所有方案等量增加预热：

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 256 --random-seed 0 \
  --temperature 0 \
  --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 --experiment-name "$EXPERIMENT-$VARIANT" \
  --wandb-group "$EXPERIMENT" --wandb-run-name "$VARIANT" \
  --output local,wandb
```

每行 JSONL 定义一组参数，`parallel`、`number` 或 `rate` 的列表会展开成负载点。同一行只能将 `parallel` 或 `rate` 中的一个设为多值列表；`number` 也是多值列表时，长度需与该轴一致。

每行可以改变负载、生成或数据集设置，包括输出长度上下界。服务身份、凭据、轨迹来源和结果去向保持不变。扫描不与轨迹回放或多数据集组合。`--num-runs` 可重复运行各参数点。

`--warmup-requests` 默认为零，也可在扫描行内通过 `warmup_requests` 设置。预热使用与测量相同的参数和起始数据，全部完成且成功后才进入测量。预热结果独立保存在每次重复的 `warmup/` 下，不计入正式指标。

每个参数点都有结果目录。`sweep_points.json` 保留每次重复；`sweep_summary.json` 和 `sweep_summary.csv` 按参数点统计均值、中位数、样本标准差及范围，并用样本数表示缺失值。各次 p95 的统计不等于合并请求后的 p95。有效点足够时，`pareto/PARETO.png` 比较每个配置用户与每张声明 GPU 的输出 token 吞吐量。再次实验时选择新的 `--experiment-name`，或省略它使用自动创建的目录。

## 对比推理配置

普通精度、量化权重和推测解码都复用上述步骤和参数文件，通过现有[推理参数](../../../docs/inference-parameters_zh.md)配置。使用同一模型的对应 checkpoint 和兼容的 tokenizer，不要将不同模型之间的差异归因于量化。

每种方案按以下步骤执行：

1. 只改变本次要评估的推理设置，保持硬件、副本数、上下文上限、其他引擎设置、tokenizer、负载和请求参数一致。比较 AWQ 权重量化时，普通精度基线使用相同且受支持的计算精度。
2. 将 `VARIANT` 改为 `awq`、`ngram` 等不同名称，保持 `EXPERIMENT` 不变。重新执行 `foretoken deploy examples/quickstart --timeout 20m`，再重复预热和测量。`bench` 复用已有服务时不会应用 YAML 修改，必须确认更新成功后再把结果记入新方案。
3. 测量完成后，在每组结果旁保存渲染后的部署：

   ```bash
   kubectl kustomize examples/quickstart > "results/$EXPERIMENT-$VARIANT/deployment.yaml"
   git rev-parse HEAD > "results/$EXPERIMENT-$VARIANT/commit.txt"
   ```

   每次重复的 `environment.json` 也会记录客户端来源；Kustomize 模式还记录运行前后观察到的服务/分组参数、Pod 镜像 ID 和节点位置。GPU 型号、驱动和推理引擎版本及本地修改仍需另行保存。期望 YAML 本身不能证明哪些 runtime 处理了请求。

直接在现有 W&B 分组中比较相同并发点，或读取各实验的 `sweep_points.json`。保留每次重复和失败记录，先检查实际输入输出长度及成功请求数，再比较输出吞吐量、TTFT、TPOT 和端到端延迟在重复运行中的中位数及范围。各次 p95 的中位数不是合并请求后的 p95。正式性能比较使用不带 profiling 的运行。

随机输入用于控制长度，不能代表推测解码在真实任务上的收益。还应使用固定且有代表性的[对话数据集](conversations_zh.md)，各方案保持相同数据行和轮次限制。将同一份扫描配置用于真实提示词时，移除行内的 `min_output_length` 和 `max_output_length`（它们要求 `--dataset random`），并设置统一的 `max_tokens` 上限。若实验要求预热数据与测量互斥，请关闭自动预热，通过独立命令使用不同的数据偏移。量化还需独立评估输出质量，HTTP 成功不等于回答正确。

需要分析耗时时，对选定负载点使用现有[评测时采集流程](../../../observability/profiling_zh.md)，通过 `foretoken profile view` 查看结果。查看或导出采集结果后，执行 `foretoken delete examples/quickstart` 清理诊断服务。

## 输出示例

单张 A100 80GB PCIe 上的 Qwen3-0.6B：

![扫描命令的实际输出](../imgs/sweep-cli.png)

W&B 按一秒完成窗口展示 E2EL p95；帕累托图比较整组吞吐量。

![按一秒完成窗口统计的 E2EL p95 时间曲线](../imgs/sweep-wandb.png)

![实测帕累托前沿](../imgs/sweep-pareto.png)
