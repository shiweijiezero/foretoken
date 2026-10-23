<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 维护 vLLM 补丁

[English](README.md) | 简体中文

在 `catalog.json` 中按应用顺序声明补丁。供固定源码和全部 Python 引擎版本共用的补丁只需一条记录：

```json
{
  "path": "common/engine-abort-completion.patch",
  "rust": true,
  "python": {}
}
```

`rust: true` 表示供 `make vllm-source` 使用。`python` 规则用于已安装引擎和引擎源码构建，`{}` 适用于全部兼容配置；可通过其中的 `profiles`、`versions`、`requires` 限定兼容配置、包版本或所需的上游文件。清单顶层的 `profiles` 定义 Python 版本与兼容配置名称的对应关系。

| 位置 | 用途 |
| --- | --- |
| `common/` | 共享的 Python、通信与原生代码补丁 |
| `compatibility/` | 适配不同上游接口的变体 |
| `rust/` | 上游 Rust crate 的补丁 |

补丁路径相对于本目录。补丁使用 `-p1`，应用目录是包含 `vllm/` 的上游仓库根目录或已安装包的父目录。

修改上游源码后重新生成 unified diff。共享行为放在 `common/`，接口差异放在 `compatibility/`。在受影响的源码版本上检查所选补丁，并实际运行改动的功能。

模型镜像通过 `apply.py`、引擎源码构建通过 `vllm_patches.py` 的 `engine_patches()` 选择 Python 规则；Rust 构建和源码维护通过 `source.rs` 准备清单中的 `rust` 条目。重复执行准备或镜像构建，检查已应用补丁的源码能否复用。

镜像和源码构建共用 `vllm_patches.py` 中的依赖适配。更新依赖时，保留引擎镜像的 NCCL override 和其他组件仍需使用的包。

其他库的补丁放在与 `vllm/` 同级的独立目录中。

`common/scheduler-load.patch` 负责统计 token 需求，各版本的调度器适配负责传入全部等待队列，并在调度更新 token 计数前采集 decode 上下文。请求被抢占后，计算进度虽然清零，其上下文需求仍需保留。

## 引擎完成契约

`rust/engine-request-completion.patch` 提供在传输提交前订阅的 `RequestCompletion`。只有引擎返回的终止输出或已完成请求通知能确认执行结束。该信号独立于输出消费者，丢弃流、收到 abort 应答或客户端本地产生 abort 输出都不能完成它。传输关闭会让观测以错误结束，不能当作成功终止。

`common/engine-abort-completion.patch` 让 Python EngineCore 在各传输路径上发布调度器已确认终止的请求 ID，包括即时取消。上游请求注册、调度终止或输出传递发生变化时，须同步核对这两个补丁。
完成信号表示请求调度结束，不表示 GPU 同步或连接器释放 KV。
修改该契约后，在受影响的传输路径上执行正常完成、取消和丢弃输出消费者的流程，同时检查独立完成信号和返回的输出。

## 设备前缀保留

[前缀目录](../../model-server/python/connectors/retained.py)供 Prefill 查找并领取 Decode 保留的 KV。传输完成前，缓存引用始终由原生 connector 持有；适配新的 vLLM 版本时，保持这一归属。

- `common/nixl-retained-prefix.patch` 统一管理普通 P→D 与保留前缀 D→P 读取。取消时关闭新授权，所有张量并行 worker 确认传输完成后才能归还缓存块。每个 reader 独立续期；reader 失联或传输状态不明时，引擎持有缓冲区并退役。保留原生握手中的生命周期版本检查。
- `common/mooncake-retained-prefix.patch` 在原生 sender loop 中管理领取、撤销和 WRITE 提交。每个已接纳的 reader 都完成 WRITE 或确认未提交 WRITE，且全部 worker 完成后，才能释放缓存。I/O 完成状态不明时使 worker 退役。闲置保留使用 `kv_lease_duration`，与发送超时分开。

通过 `catalog.json` 选择版本适配补丁，`common/kv-connector-utility.patch` 提供共用的 EngineCore 目录访问。修改后使用[模型级 P/D 配置](../../../docs/features/inference-parameters_zh.md#分离预填充与解码并复用-kv)，通过不同前端发送两轮请求，对比复用与重算的输出 token，再检查取消、到期、对端退出和张量并行完成期间的缓存引用。

## Chat token 元数据

`rust/chat-token-metadata.patch` 基于上游的[解码归属](https://github.com/vllm-project/vllm/commit/b911fe85c42ae984f27d183a0b1de309adab9bf2)和[推理内容归属](https://github.com/vllm-project/vllm/commit/21a22110404d98dcab5f51cd90e9acb0acc15e47)实现，将采样位置及完整解码范围传过解析器的缓冲阶段，也覆盖多个 token 共同解码成一个 UTF-8 片段的情况。

`ChatTokenMetadataMode::Sampled` 保留原始元数据及其输出节奏。`VisibleContent` 等待整个 token 的解码范围都被确认为解析器的可见内容后，再释放元数据；跨越隐藏内容或被排除的停止文本时，整个 token 的元数据都不输出。usage 仍按原始生成结果统计。更新补丁后，应使用真实 tokenizer 和输出处理器，在两种模式下验证停止文本缓冲、截断和解析失败后的恢复。
