# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Build the localized documentation site from the repository's public sources."""

from __future__ import annotations

import posixpath
import re
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import yaml
from markdown import Markdown
from markdown.extensions.toc import slugify_unicode
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.livereload import LiveReloadServer
from mkdocs.plugins import event_priority
from mkdocs.structure.files import Files
from mkdocs.structure.nav import Navigation
from mkdocs.structure.pages import Page

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_SOURCES = (
    "README.md", "README_zh.md", "CONTRIBUTING.md", "CONTRIBUTING_zh.md",
    "CODE_OF_CONDUCT.md", "CODE_OF_CONDUCT_zh.md", "docs", "cli",
    "benchmarks/README.md", "benchmarks/README_zh.md", "benchmarks/docs",
    "rsi-playbooks", "observability", "examples", "data-plane", "control-plane",
)
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css"}
INLINE_LINK = re.compile(r"(!?\[[^\]\n]*\]\()(<[^>\n]+>|[^\s)]+)")
REFERENCE_LINK = re.compile(r"(^ {0,3}\[[^\]\n]+\]:\s*)(<[^>\n]+>|\S+)")
HTML_LINK = re.compile(r"((?:href|src)=[\"'])([^\"']+)")
LANGUAGE_LINE = re.compile(
    r"^(?:English|\[English\]\([^)]*\))\s*\|\s*"
    r"(?:简体中文|中文|\[(?:简体中文|中文)\]\([^)]*\))(?:\s*[|·].*)?$"
    r"|^\[(?:English|简体中文|中文)\]\([^)]*\)$"
)


@event_priority(100)
def on_config(config: MkDocsConfig) -> MkDocsConfig:
    """Select the generated source tree before i18n configures language builds."""
    config.docs_dir = str(_tree.directory)
    config.mdx_configs.setdefault("toc", {})["slugify"] = slugify_unicode
    _tree.repository_url = config.repo_url.rstrip("/")
    for name in PUBLIC_SOURCES:
        path = str(ROOT / name)
        if Path(path).exists() and path not in config.watch:
            config.watch.append(path)
    return config


@event_priority(100)
def on_pre_build(config: MkDocsConfig) -> None:
    """Refresh staged sources for normal builds and live source edits."""
    _tree.refresh()


@event_priority(-100)
def on_serve(
    server: LiveReloadServer, config: MkDocsConfig, builder: Callable[[], None]
) -> LiveReloadServer:
    """Watch original sources, not the staging files rewritten by each build."""
    server.unwatch(config.docs_dir)
    return server


def on_page_markdown(markdown: str, page: Page, config: MkDocsConfig, files: Files) -> str:
    """Select the landing-page layout and link editing to each original repository file."""
    # Leading license comments prevent MkDocs from inferring the document's H1.
    heading = re.search(r"^# (.+)$", markdown, re.MULTILINE)
    if heading is not None:
        page.meta.setdefault("title", heading[1].replace("`", ""))
    source_uri = PurePosixPath(page.file.src_uri)
    original = next((source for source, destination in _tree.destinations.items() if destination == source_uri), None)
    if original is not None:
        if not (ROOT / original).is_file():
            original = PurePosixPath("rsi-playbooks/guidance/knowledge") / original.name
        page.edit_url = f"{config.repo_url}/edit/main/{original}"
    if source_uri in {PurePosixPath("index.md"), PurePosixPath("index.zh.md")}:
        page.meta["template"] = "home.html"
        page.meta["hide"] = ["navigation", "toc", "footer"]
        page.meta["search"] = {"exclude": True}
        # The custom homepage owns its sections; retain only the real title in the mobile TOC.
        return next(line for line in markdown.splitlines() if line.startswith("# "))
    return markdown


def on_page_context(
    context: dict[str, Any], page: Page, config: MkDocsConfig, nav: Navigation
) -> dict[str, Any]:
    """Supply localized interface copy to the homepage without duplicating guide content."""
    if page.meta.get("template") == "home.html":
        locale = "zh" if config.theme["language"] == "zh" else "en"
        context["home_locale"] = locale
        home = yaml.safe_load((ROOT / "docs/site/home.yml").read_text())[locale]
        context["home"] = home
        examples = []
        for example in home["examples"]:
            body = (
                f"```bash\n{example['command'].rstrip()}\n```\n\n"
                f"{example['note']}\n\n[{example['guide']}]({example['href']})\n"
            )
            examples.append(f'=== "{example["label"]}"\n\n' + textwrap.indent(body, "    "))
        context["home_examples"] = Markdown(
            extensions=config.markdown_extensions, extension_configs=config.mdx_configs
        ).convert("\n".join(examples))
    return context


def on_shutdown() -> None:
    """Release the generated source tree when the documentation server stops."""
    _tree.temporary.cleanup()


class DocumentationTree:
    """Own generated source files for MkDocs builds and development-server reloads."""

    def __init__(self) -> None:
        """Create the staging tree retained across builds and released by MkDocs shutdown."""
        temporary_root = ROOT / "tmp" / "docs-site"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = TemporaryDirectory(prefix="source-", dir=temporary_root)
        self.directory = Path(self.temporary.name)
        self.destinations: dict[PurePosixPath, PurePosixPath] = {}
        self.repository_url = ""

    def refresh(self) -> None:
        """Stage public Markdown and assets without copying private or ignored trees."""
        names = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *PUBLIC_SOURCES],
            cwd=ROOT,
        ).decode().split("\0")
        sources = {
            PurePosixPath(name) for name in names
            if name and (ROOT / name).is_file()
            and (
                Path(name).suffix == ".md"
                or Path(name).suffix in ASSET_SUFFIXES
            )
        }
        documents = {source: (ROOT / source).read_text() for source in sources if source.suffix == ".md"}
        self.add_knowledge_indexes(documents)
        self.destinations = {source: self.destination(source) for source in sources | documents.keys()}
        self.destinations[PurePosixPath("docs/site/.nav.yml")] = PurePosixPath(".nav.yml")
        retained = set()
        for source, destination in self.destinations.items():
            data = (
                self.markdown(documents[source], source).encode()
                if source.suffix == ".md" else (ROOT / source).read_bytes()
            )
            target = self.directory / destination
            retained.add(target)
            # Language builds share this tree; preserve unchanged files between passes.
            if target.is_file() and target.read_bytes() == data:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for target in self.directory.rglob("*"):
            if target.is_file() and target not in retained:
                target.unlink()

    def markdown(self, content: str, source: PurePosixPath) -> str:
        """Rewrite document links while leaving command and configuration examples intact."""
        result = []
        fence = ""
        for line in content.splitlines(keepends=True):
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if fence:
                result.append(line)
                if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                    fence = ""
                continue
            if marker:
                fence = marker[1]
                result.append(line)
                continue
            if LANGUAGE_LINE.fullmatch(line.strip()):
                continue
            for pattern in (INLINE_LINK, REFERENCE_LINK, HTML_LINK):
                line = pattern.sub(lambda match: match[1] + self.link(match[2], source), line)
            result.append(line)
        return "".join(result)

    def destination(self, source: PurePosixPath) -> PurePosixPath:
        """Map a public source to the site's layout and native language suffix."""
        if source.suffix == ".md":
            chinese = source.stem.endswith("_zh")
            base = source.with_name(source.stem.removesuffix("_zh") + ".md")
            base = {
                PurePosixPath("README.md"): PurePosixPath("quickstart.md"),
                PurePosixPath("docs/README.md"): PurePosixPath("index.md"),
            }.get(base, base)
            return base.with_suffix(".zh.md") if chinese else base
        if source.is_relative_to("docs/site/assets"):
            return PurePosixPath("assets") / source.relative_to("docs/site/assets")
        return source

    def add_knowledge_indexes(self, documents: dict[PurePosixPath, str]) -> None:
        """Make the existing knowledge directory links browsable without maintaining duplicate indexes."""
        knowledge = PurePosixPath("rsi-playbooks/guidance/knowledge")
        entries = tuple(documents.items())
        for suffix in ("", "_zh"):
            index = knowledge / f"README{suffix}.md"
            for title, directory, description in re.findall(
                r"^- \[([^\]]+)\]\(([^)]+/)\)[:：]\s*(.+)$",
                documents[index], re.MULTILINE,
            ):
                category = knowledge / directory
                page = category / f"README{suffix}.md"
                if page in documents:
                    continue
                links = []
                for source, content in sorted(entries):
                    if source.parent != category or source.stem.endswith("_zh") != bool(suffix):
                        continue
                    heading = re.search(r"^# (.+)$", content, re.MULTILINE)[1]
                    links.append(f"- [{heading}]({source.name})")
                documents[page] = f"# {title}\n\n{description}\n\n" + "\n".join(links) + "\n"

    def link(self, value: str, source: PurePosixPath) -> str:
        """Resolve repository links to localized pages, shared assets, or source on GitHub."""
        angled = value.startswith("<") and value.endswith(">")
        raw = value[1:-1] if angled else value
        url = urlsplit(raw)
        if url.scheme or url.netloc or not url.path:
            return value
        path = unquote(url.path)
        target = PurePosixPath(posixpath.normpath(
            path.lstrip("/") if path.startswith("/") else str(source.parent / path)
        ))
        if (ROOT / target).is_dir():
            readme = target / ("README_zh.md" if source.stem.endswith("_zh") else "README.md")
            if readme in self.destinations:
                target = readme
        if target in self.destinations:
            destination = self.destinations[target]
            # i18n resolves canonical Markdown paths to the active language or its fallback.
            if destination.name.endswith(".zh.md"):
                destination = destination.with_name(destination.name.removesuffix(".zh.md") + ".md")
            relative = posixpath.relpath(destination, self.destinations[source].parent)
            result = urlunsplit(("", "", quote(relative, safe="/"), url.query, url.fragment))
        elif (ROOT / target).exists():
            kind = "tree" if (ROOT / target).is_dir() else "blob"
            result = f"{self.repository_url}/{kind}/main/{quote(str(target), safe='/')}"
            if url.query:
                result += "?" + url.query
            if url.fragment:
                result += "#" + url.fragment
        else:
            return value
        return f"<{result}>" if angled else result


_tree = DocumentationTree()
