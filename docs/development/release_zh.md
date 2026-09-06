<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 发布版本

Foretoken 的 Python distribution 使用 Python 的版本规范；GitHub tag、OCI 镜像和 Helm Chart 使用相同的版本标识。

| 阶段 | 版本 | GitHub tag | PyPI 安装 |
| --- | --- | --- | --- |
| Alpha | `0.0.1a1` | `v0.0.1a1` | `pip install --pre foretoken` |
| Beta | `0.0.1b1` | `v0.0.1b1` | `pip install --pre foretoken` |
| Release Candidate | `0.0.1rc1` | `v0.0.1rc1` | `pip install --pre foretoken` |
| 正式版 | `0.0.1` | `v0.0.1` | `pip install foretoken` |

`pip` 默认不会选择预发布版本。安装 Alpha、Beta 或 Release Candidate 时使用 `--pre`，也可以指定精确版本，例如 `foretoken==0.0.1a1`。

源码安装与发布版本分开：

```bash
pip install -e .
```

## OCI 镜像 tag

Foretoken 镜像和 Helm Chart 使用相同的发布版本标识：

```text
0.0.1a1
0.0.1b1
0.0.1rc1
0.0.1
```

`latest` 是可变的开发别名。正式部署、回滚和可复现安装应使用具体版本 tag。

## 发布顺序

1. 将 `pyproject.toml` 更新为下一个 PEP 440 版本。
2. 构建并验证 Python distribution 和受影响的 OCI 镜像。
3. 推送对应的镜像和 Helm Chart tag。
4. 创建对应的 GitHub tag 和 Release。
5. 由发布 workflow 将 Python distribution 上传到 PyPI。
6. 验证已发布的 package、镜像、Chart 和全新安装路径。
