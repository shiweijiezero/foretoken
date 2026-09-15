<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 发布版本规则

[English](release.md) | 简体中文

Foretoken 会发布 Python distribution、OCI 镜像和 Helm Chart。Python package 遵循 PEP 440，OCI 镜像与 Helm Chart 使用 SemVer。两种格式的具体写法不同，但同一次发布的阶段和序号必须一致。

## 版本阶段

| 阶段 | 用途 | Python 版本 | OCI 与 Helm 版本 |
| --- | --- | --- | --- |
| Development | 可识别的本地或 CI 开发快照 | `0.0.1.dev1` | `0.0.1-dev.1` |
| Alpha | 早期集成与接口验证 | `0.0.1a1` | `0.0.1-alpha.1` |
| Beta | 功能完成后的兼容性与部署验证 | `0.0.1b1` | `0.0.1-beta.1` |
| Release Candidate | 正式发布前的最终验证 | `0.0.1rc1` | `0.0.1-rc.1` |
| Stable | 面向普通安装路径的正式版本 | `0.0.1` | `0.0.1` |
| Python post-release | 修正已发布的 Python 产物或其 metadata | `0.0.1.post1` | 通常复用 `0.0.1`；平台产物变化时发布下一 patch |

同一阶段再次发布时递增末尾序号，例如 `0.0.1a2`、`0.0.1b2` 或 `0.0.1rc2`。只有当前版本达到下一阶段的用途时，才进入下一阶段。

Python 版本的先后顺序如下：

```text
0.0.1.dev1 < 0.0.1a1 < 0.0.1b1 < 0.0.1rc1 < 0.0.1 < 0.0.1.post1
```

Development 版本只用于本地或 CI 快照，不创建 GitHub Release，也不上传 PyPI。OCI 镜像可以额外维护 `latest` 作为日常源码迭代使用的可变别名；`latest` 不是 Helm Chart 版本，也不是发布版本。

## 安装 Python 版本

Alpha、Beta 和 Release Candidate 都是预发布版本。`pip` 默认不会选择这些版本，需要显式允许预发布版本或指定精确版本：

```bash
pip install --pre foretoken
pip install foretoken==0.0.1a1
```

Stable 和 post-release 使用普通安装命令：

```bash
pip install foretoken
```

`.postN` 通常复用对应 Stable 版本的平台产物，因为它只修正已发布的 Python package 或 metadata，不承载常规代码变化。如果运行行为或平台产物需要变化，应发布下一 patch，例如 `0.0.2`，而不是把这些变化放进 `.postN`。

从仓库安装源码与发布版本相互独立：

```bash
pip install -e .
```

## Tag 与版本来源

GitHub Release 使用带 `v` 前缀的 Python 版本，因为发布 workflow 会根据该 Release 上传对应的 Python distribution：

```text
v0.0.1a1
v0.0.1b1
v0.0.1rc1
v0.0.1
v0.0.1.post1
```

每类产物只有一个权威版本来源：

- `pyproject.toml` 负责 Python distribution 版本。
- `deploy/charts/foretoken/Chart.yaml` 负责 Helm `version` 和 `appVersion`。
- Foretoken OCI 镜像和 Helm Chart package 使用同一发布阶段对应的 SemVer 值。

已经发布的版本不可覆盖。不得重新构建并覆盖 PyPI 或 OCI registry 中已经存在的版本；应根据实际情况递增 Development、预发布、post-release 或 patch 序号。

## 构建与推送发布产物

在待发布的源码目录执行。准备 Python 3.11+、已安装的 Foretoken 包（`pip install -e .`）、Git、Rust/Cargo、Make、支持 BuildKit 的 Docker 和 Helm，以及兼容的 NVIDIA、沐曦推理运行时镜像。沐曦基础镜像可按[镜像构建指南](metax-platform_zh.md#构建镜像)准备。将下面的仓库前缀和基础镜像替换为实际值：

```bash
export REGISTRY=ghcr.io/your-org/foretoken
export INFERENCE_ENGINE_IMAGE=your-nvidia-runtime:version
export METAX_INFERENCE_ENGINE_IMAGE=your-metax-runtime:version

deploy/release-artifacts build --registry "$REGISTRY"
```

该命令构建共用的 `control-plane`、`frontend`，以及 NVIDIA 的 `model-server:<version>` 和沐曦的 `model-server:<version>-metax`，并将 Chart 打包到 `/tmp/foretoken-release`。版本取自 `pyproject.toml` 和 `Chart.yaml`，无需另传 tag。CPU 架构兼容时，同一台机器可构建两个变体，不必同时安装两种 GPU；运行验证仍分别在对应硬件上进行。

基础镜像需要特定 Python 解释器时，NVIDIA 设置 `FORETOKEN_VLLM_PYTHON`，沐曦设置 `METAX_VLLM_PYTHON`。已有的构建镜像源和软件包索引变量继续生效。

完成产物验证后，登录仓库并推送：

```bash
docker login ghcr.io
helm registry login ghcr.io
deploy/release-artifacts push --registry "$REGISTRY"
```

已有远端 tag 保持不变，中途失败后可用相同命令继续。此入口不更新 `latest`，不创建 GitHub Release，也不发布 Python 包。首次发布时配置 package 可见性，公开发布需验证匿名访问。

`--components model-server,model-server-metax` 可选择产物；修改 Chart 目录时，在构建和推送命令中使用相同的 `--output-dir`。`--dry-run` 仅打印命令，完整选项见 `deploy/release-artifacts --help`。

## 发布顺序

1. 确定发布阶段，并按上表更新 `pyproject.toml` 和 `Chart.yaml`。
2. 构建并验证 Python distribution、Helm Chart 和受影响的 OCI 镜像。
3. 推送对应的 OCI 镜像和 Helm Chart tag。
4. 在实际构建并验证产物的提交上打 tag，发布 GitHub Release，简述重要改动，并具名感谢贡献者及其提供的支持。
5. 由发布 workflow 将 Python distribution 上传到 PyPI。
6. 验证已发布的 package、镜像、Chart 和全新安装路径。
