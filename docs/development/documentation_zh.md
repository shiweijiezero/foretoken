<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 预览和构建文档

[English](documentation.md) | 简体中文

准备 Python 3.11 或更高版本及 [uv](https://docs.astral.sh/uv/getting-started/installation/)，在仓库根目录启动文档站：

```bash
make docs-serve
```

打开命令打印的本地 URL。修改公开文档后，页面会自动重新加载。通过页头切换中英文，用搜索框查找命令和指南。需要换一个本地端口时，执行 `make docs-serve DOCS_ADDR=127.0.0.1:8001`。

## 修改页面

在内容所属目录编辑已有 Markdown：CLI 用法在 `cli/`，优化指导在 `rsi-playbooks/`，部署指南在 `docs/deployment/`。英文页和对应的 `_zh.md` 文件保持一致，链接和图片继续使用相对于原文件的路径。

仓库根 README 提供快速开始，`docs/README.md` 和 `docs/README_zh.md` 继续作为仓库中的文档索引。各篇指南直接使用所属目录的 Markdown 原文构建。

新增导航项时，修改 `docs/site/.nav.yml`，并在 `mkdocs.yml` 的 `nav_translations` 中补充中文名称。现有栏目用限定目录的通配规则纳入详细指南。知识分类页由知识索引和文章标题生成，分类链接可直接在站内浏览。

网站首页的布局、双语界面文案和通用样式位于 `docs/site/`。首页示例与指南共用 Material 的标签页、语法高亮和复制功能。

## 构建静态站点

```bash
make docs
```

生成结果位于 `tmp/docs-site/site/`，可通过静态 Web 服务器访问，中文站位于 `zh/` 下。文档构建依赖与 Foretoken 运行依赖分别安装。提交 Markdown、导航或主题改动，生成文件保留在本地构建目录。
