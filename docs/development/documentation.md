<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Preview and build documentation

English | [简体中文](documentation_zh.md)

From the repository root, use Python 3.11 or later and [uv](https://docs.astral.sh/uv/getting-started/installation/) to start the documentation site:

```bash
make docs-serve
```

Open the printed local URL. The server reloads when public documentation changes. Use the header's language selector to switch between English and Chinese, and its search box to find commands and guides. To use another local port, run `make docs-serve DOCS_ADDR=127.0.0.1:8001`.

## Edit a page

Edit the existing Markdown in its owning directory: CLI usage in `cli/`, optimization guidance in `rsi-playbooks/`, and deployment guides in `docs/deployment/`. Keep the English page and its `_zh.md` counterpart aligned. Relative links and images remain relative to those source files.

The repository-root README provides the Quick Start, and `docs/README.md` remains the repository's documentation index. Guides are built directly from their original Markdown.

Edit `docs/site/.nav.yml` to add a navigation entry, and add its Chinese label under `nav_translations` in `mkdocs.yml`. Existing sections use scoped globs to include their detailed guides. Knowledge category pages are built from the knowledge index and article titles, so category links stay within the site.

The website's homepage layout, localized interface copy, and shared styles live in `docs/site/`. Homepage examples use the same Material tabs, syntax highlighting, and copy controls as the guides.

## Build a static site

```bash
make docs
```

The output is written to `tmp/docs-site/site/`. It can be served by a static web server; the Chinese site is under `zh/`. Documentation dependencies are installed separately from the Foretoken runtime. Keep generated files out of commits and submit the Markdown, navigation, or theme changes instead.
