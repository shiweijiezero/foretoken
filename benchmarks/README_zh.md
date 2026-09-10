# 评测

[English](README.md) | 简体中文

使用 `foretoken bench` 测量 Foretoken 部署或现有 OpenAI 兼容端点的延迟和吞吐量。

## 开始前

从仓库根目录使用 Python 3.10 或更高版本运行评测命令：

```bash
pip install 'foretoken[bench]'

# 如果使用源码安装：
# pip install -e .
# pip install -e '.[bench]'
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

## 按需 profiling

只对 Foretoken Kubernetes 部署中的少量请求做 profiling：

```bash
foretoken bench examples/quickstart \
  --profile \
  --number 2 \
  --output local
```

命令准备正常的 benchmark 负载，在即将发送请求时，向选定服务的 model-server Pod 提交一个采样窗口。运行时等待 `--profile-delay` 秒（默认 `0`），采集最多 `--profile-duration` 秒（默认 `5`），然后自行停止并导出，不依赖本机继续保持连接。不会额外发送预热请求。延迟期间照常发送请求；如果 benchmark 提前结束，命令会取消剩余采样。若负载在延迟期间就结束，本次只有状态报告，没有 trace。原生 profiler 启动也需要时间，因此非常短的负载可能在捕捉到推理活动前就结束。

采样控制通过 Kubernetes exec 调用 Pod 内已有的管理监听端口，不新增 profiling 端口转发、Service 或 YAML 设置。你的 Kubernetes 身份需要 Pod exec 权限。命令和 model-server 镜像需使用相匹配的当前源码版本；源码镜像构建会自动包含所需的 vLLM profiling 修复。只有立即 start/stop 接口的旧镜像不支持窗口提交。普通 benchmark 的 Frontend 访问方式不变。

结果自动保存到 `results/profiles/<capture-id>/<pod>/`，包含 `capture.json` 和原生 trace。文件导出时间不计入采样时长；只有确认复制成功的文件才会从 Pod 删除。若无法确认停止、导出或下载成功，命令会报错，并保留本次 benchmark 创建的部署供恢复结果。`.pt.trace.json.gz` 可直接放入 [Perfetto](https://ui.perfetto.dev/) 查看。

Profiling 会拖慢推理并产生较大的文件，因此负载应保持很小。`--profile` 必须搭配 Kustomize 部署路径，不能与 `--url` 或 `--bench-params` 组合。同一个 model-server 同时只接受一次采样。目前这条路径实现单个 Torch 窗口；周期重复、独立 profiling 命令、Nsight 和沐曦验证仍待后续完成。

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
