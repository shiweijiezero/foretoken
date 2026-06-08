# Foretoken

> Industrial LLM inference on vLLM, with plugin-based optimization and real-workload evaluation.

基于 vLLM 的推理优化项目:在工业级真实推理之上,以树外插件做各项优化(KV 管理、MTP 等),并用真实业务负载检验每一项优化的好坏，以服务Token工厂。

## 评测数据准备

把真实生产时序与真实多轮对话缝合为可复现的评测负载,已发布为公开数据集 [`weijiezz/foretoken-trace`](https://huggingface.co/datasets/weijiezz/foretoken-trace):时序、并发与多轮会话结构取自 Mooncake 生产 trace,对话内容取自真实多轮对话并在会话内逐轮累积,每行 `{timestamp_ms, prompt}`。三个 split 各保留各自约 1 小时的真实时序:`conversation`(约 11k 请求)、`mooncake` / `toolagent`(各约 20k 请求;后两者内容同源、到达时序不同)。

数据准备命令:
```bash
pip install -e .
bash scripts/build_dataset.sh
```

## 评测 benchmark

闭环回放真实会话负载:按真实 timestamp 回放、模型现场生成回复拼下一轮(非预录答案),采集 TTFT/TPOT/E2E、吞吐与 goodput。默认在回放进程内自起引擎(`AsyncLLM`,退出即释放 GPU);也可打 `vllm serve`(三种后端形式见下)。

```bash
pip install -e .   # 装依赖(含 vLLM;需 GPU 机器)
# 权重从 HF 拉取,数据集默认公开的 weijiezz/foretoken-trace,
# 不传 --config 即用 config/models/default.toml(贪心解码) 换更大的模型时配上它的 `config`(采样 + 引擎并行)
CUDA_VISIBLE_DEVICES=0 bash scripts/bench.sh \
  --model Qwen/Qwen3-4B-Instruct-2507 \
  --split conversation --window 0:10 --rate 20
```

`--window 0:10 --rate 20` = 回放第 0–10 分钟、到达率 20 req/min(换算 total_requests = rate × 窗口分钟数 = 200)。换更大模型配上其 `config`(采样 + 引擎并行)与多卡 `CUDA_VISIBLE_DEVICES`。

### 三种后端形式

闭环回放本身(时间调度 / 会话化 / 现场拼下一轮)三种形式一致,区别只在请求发往哪里、测到哪一层:

| 后端 | 测什么 | 何时用 |
|---|---|---|
| **进程内**(默认) | 纯引擎能力(KV / 调度 / 解码)+ 逐 iteration KV%/并发监控 | 优化 A/B(KV/MTP 插件对照),隔离引擎、开销最低 |
| **`--endpoint URL`** | 全生产栈(HTTP + 前端 tokenize/detok + `--api-server-count` + DP 路由) | 部署级真实容量;`vllm serve` 由你自管 |
| **`--serve`** | 同上,但 bench 自起 vllm serve、回放完整组 kill 释放 GPU | 一条命令端到端、自动收尾 server |

```bash
# 打已有 vllm serve(自己先 vllm serve <model> --port 8000)
bash scripts/bench.sh --endpoint http://localhost:8000 --model <served-name> \
  --split conversation --window 0:10 --rate 20 --gpus <服务器卡数>

# 自起 vllm serve、跑完自动关停(引擎配置取 config [serve])
CUDA_VISIBLE_DEVICES=0,1,2,3 bash scripts/bench.sh --serve --dp 4 --api-server-count 4 \
  --model <weights> --config config/models/<m>.toml \
  --split conversation --window 0:10 --rate 20
# 全部参数与默认值:python -m foretoken.bench.replay --help
```

> 进程内只测引擎(单 bench 进程做全部 tokenize/detok,高并发下客户端可能先于 GPU 封顶);API 形式把前端移到 `vllm serve` 侧(`--api-server-count` 可多开),用以区分引擎与前端瓶颈。API 形式无逐 iteration 引擎监控,`--gpus` 给服务器卡数算 goodput/GPU。

每 run 落 `runs/<…>/`:`summary.md`(markdown 摘要 + 内嵌图)、`run.json` / `turns.jsonl` / `engine_stats.jsonl`、`en/`·`zh/` 双语图;`runs/INDEX.md` 排行榜。逐轮输入输出由 `--cases` 控制(默认仅可读样例 `cases.md`,`full` 另出全量 `cases.jsonl`,`off` 不存)。完整命令查看 [`docs/07`](docs/07-eval-playbook.md)。

指标分四组(`summary.md` / `run.json`):

- **延迟**:TTFT(首 token)、TPOT(逐 token 间隔)、E2E(整轮)的 p50/p90/p99,及尾部比 p99/p50。
- **吞吐**:输出 / 输入 / 总 tok/s(及 /GPU)、完成 req/s。
- **goodput**:按 SLO 阶梯(严/中/松,TTFT+TPOT 双门限)只计达标轮的 tok/s,及 /GPU、归一化 tok/(s·GPU字节);阶梯可用 `--slo TTFT_ms:TPOT_ms` 自定义。
- **SchedulerStats**:KV cache 利用率、在飞 / 排队请求数随时间。

> 真实 trace 是集群级到达,单实例 1× 全量回放会过载;用 `--rate`(或 `--total-requests`)会话级下采样把负载匹配到硬件,扫不同到达率得 goodput-vs-load 曲线、拐点即可持续容量。

## License

[Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0)。
