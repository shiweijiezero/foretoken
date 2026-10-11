<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 维护 vLLM 补丁

[English](README.md) | 简体中文

| 位置 | 用途 |
| --- | --- |
| `common/` | 共享的 Python、通信与原生代码补丁 |
| `compatibility/` | 适配不同上游接口的补丁清单与插入位置 |
| `rust/` | 上游 Rust crate 的补丁 |
| `source.series` | 固定源码版本的补丁顺序，由 `make vllm-source` 读取 |
| `version-map.yaml` | 已安装 Python 包版本到兼容补丁清单的映射 |

清单中的路径相对于本目录。补丁使用 `-p1`，应用目录是包含 `vllm/` 的上游仓库根目录或已安装包的父目录。

修改对应的上游源码后重新生成 unified diff。共享行为放在 `common/`，兼容补丁只保留有差异的导入、接口和插入位置。多个包版本可以使用同一份清单；扩展版本映射前，先在对应源码上检查整组补丁，并实际运行受影响的功能。

模型镜像通过 `apply.py` 选择清单、应用缺失补丁并编译改动的 Python 文件。Rust 源码准备直接读取 `source.series`，不按 Python 包版本选择。再次执行源码准备或镜像构建，检查已应用补丁的源码能否复用。

镜像和源码构建共用 `vllm_patches.py` 中的依赖适配。更新依赖时，保留引擎镜像的 NCCL override 和其他组件仍需使用的包。

其他库的补丁放在与 `vllm/` 同级的独立目录中。

## 引擎完成契约

`rust/engine-request-completion.patch` 提供在传输提交前订阅的 `RequestCompletion`。只有引擎返回的终止输出或已完成请求通知能确认执行结束。该信号独立于输出消费者，丢弃流、收到 abort 应答或客户端本地产生 abort 输出都不能完成它。传输关闭会让观测以错误结束，不能当作成功终止。

`common/engine-abort-completion.patch` 让 Python EngineCore 在各传输路径上发布调度器已确认终止的请求 ID，包括即时取消。上游请求注册、调度终止或输出传递发生变化时，须同步核对这两个补丁。
完成信号表示请求调度结束，不表示 GPU 同步或连接器释放 KV。模型服务的容量回收与阶段交接见[准入生命周期](../../../docs/development/admission-rules_zh.md)。

修改该契约后，在受影响的传输路径上执行正常完成、取消和丢弃输出消费者的流程，同时检查独立完成信号和返回的输出。
