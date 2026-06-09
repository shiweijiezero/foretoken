# 快速开始

在一台 GPU 机器上装好 Foretoken,准备评测数据,跑通第一个 goodput 评测。

## 前置条件

- 一台带 NVIDIA GPU 的 Linux 机器(参考后端 vLLM 需要 GPU)。
- Python ≥ 3.12。
- 模型权重从 Hugging Face 拉取;首个示例用 `Qwen/Qwen3-4B-Instruct-2507`(单卡可跑)。

## 安装

```bash
pip install -e .
```

依赖含参考后端 vLLM。CUDA wheel 随机器 driver 选择,详见 vLLM 安装文档。

## 准备评测数据

默认使用公开数据集 [`weijiezz/foretoken-trace`](https://huggingface.co/datasets/weijiezz/foretoken-trace),无需自建,`bench.sh` 会自动拉取。若要在本地重建:

```bash
bash scripts/build_dataset.sh
```

数据集把真实生产时序(Mooncake trace)与真实多轮对话内容缝合,保留真实到达节奏与会话内累积复用。三个 split:`conversation`、`mooncake`、`toolagent`。构建方法见[负载画像](workload-profiles.md)。

## 跑第一个评测

进程内自起引擎、回放第 0–10 分钟、到达率 20 req/min:

```bash
CUDA_VISIBLE_DEVICES=0 bash scripts/bench.sh \
  --model Qwen/Qwen3-4B-Instruct-2507 \
  --split conversation --window 0:10 --rate 20
```

- `--window 0:10`:回放数据集第 0–10 分钟的时间片。
- `--rate 20`:目标到达率 20 req/min(会话级下采样把集群级负载匹配到单实例;`total_requests = rate × 窗口分钟数`)。
- 不传 `--config` 时用 `config/models/default.toml`(贪心解码)。换更大的模型时配上其 `config`(采样 + 引擎并行)与多卡 `CUDA_VISIBLE_DEVICES`。

全部参数与默认值:

```bash
python -m foretoken.bench.replay --help
```

## 看产出

每次 run 落一个目录 `results/runs/<时间>__<model>__<tag>__<split>_<window>/`:

- `summary.md` — markdown 摘要 + 内嵌双语图(先看这个)。
- `run.json` — 配置与聚合指标(机器可读)。
- `turns.jsonl` — 每轮原始指标(可事后换 SLO 重算 / 重画图)。
- `engine_stats.jsonl` — 逐 iteration 引擎 KV%/运行中/排队(进程内后端)。
- `en/`、`zh/` — 双语图(CDF / 直方图 / 时间线)。

跨 run 排行榜在 `results/runs/INDEX.md`。

## 下一步

- 扫不同 `--rate` 得到 goodput-vs-load 曲线、定位拐点(可持续容量):见[评测指南](evaluation.md)。
- 读懂每个指标:见[指标定义](metrics.md)。
- 用三种后端形式区分引擎与前端瓶颈、对比多组实验:见[评测指南](evaluation.md)。
