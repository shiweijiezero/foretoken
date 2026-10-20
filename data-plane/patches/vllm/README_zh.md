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

模型镜像通过 `apply.py`、引擎源码构建通过 `vllm_patches.py` 的 `engine_patches()` 选择 Python 规则；Rust 源码准备选择清单中的 `rust` 条目。重复执行准备或镜像构建，检查已应用补丁的源码能否复用。

镜像和源码构建共用 `vllm_patches.py` 中的依赖适配。更新依赖时，保留引擎镜像的 NCCL override 和其他组件仍需使用的包。

其他库的补丁放在与 `vllm/` 同级的独立目录中。

`common/scheduler-load.patch` 负责统计 token 需求，各版本的调度器适配负责传入全部等待队列，并在调度更新 token 计数前采集 decode 上下文。请求被抢占后，计算进度虽然清零，其上下文需求仍需保留。

## 引擎完成契约

`rust/engine-request-completion.patch` 提供在传输提交前订阅的 `RequestCompletion`。只有引擎返回的终止输出或已完成请求通知能确认执行结束。该信号独立于输出消费者，丢弃流、收到 abort 应答或客户端本地产生 abort 输出都不能完成它。传输关闭会让观测以错误结束，不能当作成功终止。

`common/engine-abort-completion.patch` 让 Python EngineCore 在各传输路径上发布调度器已确认终止的请求 ID，包括即时取消。上游请求注册、调度终止或输出传递发生变化时，须同步核对这两个补丁。
完成信号表示请求调度结束，不表示 GPU 同步或连接器释放 KV。
修改该契约后，在受影响的传输路径上执行正常完成、取消和丢弃输出消费者的流程，同时检查独立完成信号和返回的输出。

## 设备前缀保留

NIXL 与 Mooncake 共用[前缀目录](../../model-server/python/connectors/retained.py)的查询和领取流程，KV 引用与传输完成仍由原生 connector 管理。`common/kv-connector-utility.patch` 通过已有 EngineCore utility 通道提供目录入口，`catalog.json` 按引擎接口选择对应的生命周期补丁。

`common/nixl-retained-prefix.patch` 让原生 pull scheduler 统一管理普通 P→D 与保留前缀 D→P 的来源。授权、取消和过期回收串行执行；先关闭新授权，再请求 worker 释放，最后等待原生 TP 完成汇合后归还缓存块。每个已授权 reader 独立续期；心跳失联或无法确认原生传输状态时，引擎持有缓冲区并退役。取消会阻止迟到回调再次提交 READ，已有 handle 仍等待原生终态。原生握手按协议版本拒绝不支持这套生命周期的对端，即使关闭布局兼容检查也不放行。0.30 兼容补丁提供同一实现所需的对端替换接口。

`common/mooncake-retained-prefix.patch` 在原生 sender loop 中串行化领取、撤销和 WRITE 提交。闲置来源使用 `kv_lease_duration`，与普通发送的超时分开。撤销和到期回收都要求已经接纳的 reader 全部完成 WRITE，或确认没有提交 WRITE；尚未开始的 reader 随后按未命中处理。无法确认 I/O 已停止的错误会使 worker 退役。接收端汇合所有来源的终态后报告加载结果，scheduler 再等待全部 TP worker 完成才释放块。

更新这些补丁后，通过不同前端执行两轮 P/D 生成，对比复用与重算的输出 token，并在取消、到期、对端退出和 TP 完成期间检查 allocator 引用。传输选择使用[模型级 P/D 配置](../../../docs/features/inference-parameters_zh.md#分离预填充与解码并复用-kv)。
