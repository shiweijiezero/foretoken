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

其他库的补丁放在与 `vllm/` 同级的独立目录中。
