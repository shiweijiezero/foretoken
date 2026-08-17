# Benchmark

[English](README.md) | 简体中文

Foretoken Benchmark 通过 OpenAI-compatible API 测量请求延迟、TTFT、TPOT 和吞吐量。同一套 CLI 既能测试已有服务，也能让 Kubernetes 临时部署一套 Foretoken 服务后在集群内完成测试。

## 安装

在源码目录中安装：

```bash
python -m pip install --upgrade pip
python -m pip install ./benchmarks
```

统一入口：

```text
foretoken bench run (--base-url URL | --deploy PATH) [OPTIONS]
foretoken bench cleanup RUN_ID
```

## 测试已有服务

提供 OpenAI-compatible `/v1` 地址和模型名：

```bash
foretoken bench run \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-0.6B
```

默认发送 100 次 `Hello`，并发为 1。可以指定 prompt 和负载：

```bash
foretoken bench run \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-0.6B \
  --prompt "Explain prefix caching" \
  --num-requests 200 \
  --max-concurrency 16
```

需要认证时通过环境变量传 key。CLI 没有 `--api-key`，结果文件也不会保存 key：

```bash
OPENAI_API_KEY=... foretoken bench run \
  --base-url https://inference.example.com/v1 \
  --model Qwen/Qwen3-0.6B
```

## Workload 与实验记录

`--dataset` 可以是本地 JSONL、Hugging Face 数据集（例如 `org/name:split`）或 `random`。多个本地/Hugging Face 数据集会依次执行，并共同使用 `--num-requests`。

```bash
foretoken bench run \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-0.6B \
  --dataset workload.jsonl \
  --num-requests 200
```

随机 workload 需要 tokenizer：

```bash
foretoken bench run \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-0.6B \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 \
  --max-prompt-length 512
```

加上 `--wandb` 可以把运行记录到 Weights & Biases。更多本地、Hugging Face、随机和多数据集示例见 [examples](doc/examples.md)。

## 临时部署后测试

这个方式适合已经安装 Foretoken control plane 的 Kubernetes 集群。`--deploy` 接受一个 YAML 文件或 Kustomize 目录，其中需要有一个 `FrontendService` 和至少一个 `ModelService`。CLI 会创建独立的 Namespace、PVC 和 Benchmark Job，再把结果取回本地。

先构建 Benchmark 镜像，并让集群每个节点都能获得它。一般做法是推送到 registry：

```bash
make image-benchmark
docker tag foretoken-benchmark:dev registry.example.com/foretoken/benchmark:dev
docker push registry.example.com/foretoken/benchmark:dev
export FORETOKEN_BENCHMARK_IMAGE=registry.example.com/foretoken/benchmark:dev
```

然后直接运行：

```bash
foretoken bench run --deploy examples/quickstart
```

如果清单中只有一个 `ModelService`，模型名会自动读取；多个模型时明确选择：

```bash
foretoken bench run \
  --deploy path/to/workload \
  --model Qwen/Qwen3-0.6B \
  --num-requests 200 \
  --max-concurrency 16
```

集群需要默认 `StorageClass`；没有时显式指定：

```bash
foretoken bench run \
  --deploy examples/quickstart \
  --storage-class local-path
```

### 资源生命周期

CLI 会先等待所选 `ModelService` Ready，并确认 Frontend 已加载可路由的 serving snapshot，之后才启动 Benchmark Job。成功时，结果回收到本地后临时 Namespace 会自动删除。失败时资源会保留，方便检查：

```bash
foretoken bench cleanup bench-20260817-143210-a1b2c3
```

成功后也想保留资源时使用 `--keep`。当前部署模式只接收一个本地 JSONL workload；Hugging Face、随机和多数据集 workload 可直接针对已有服务运行。

## 负载控制

```text
--num-requests N       总请求数，默认 100
--max-concurrency N    最大同时在途请求数，默认 1
--request-rate R       Poisson 到达率；-1 表示不限制发送速率
--open-loop            不限制同时在途请求数
--max-tokens N         每次请求最多生成的 token 数，默认 128
--temperature T        sampling temperature，默认 0
--no-stream            使用非流式响应；此时不提供 TTFT/TPOT
```

默认是 closed-loop：`--max-concurrency` 限制在途请求数。设置 `--request-rate` 后按 Poisson 过程发请求；再加 `--open-loop` 可去掉并发上限。

## 结果文件

每次运行写入：

```text
results/<run-id>/
├── manifest.json       # 运行与资源清理状态
├── config.json
├── raw-output.json
├── metrics.json
└── logs/              # 部署模式下包含 Job 与 Kubernetes 诊断
```

`manifest.json` 只记录 run ID、执行方式和资源清理状态，`cleanup` 用它确认 Kubernetes 资源归属；性能结果在 `metrics.json` 和 `raw-output.json` 中。`raw-output.json` 保存每次请求的状态、耗时、token 数和错误信息，不保存模型回答正文。多数据集运行还会为每个数据源建立子目录，使用相同的文件名。
