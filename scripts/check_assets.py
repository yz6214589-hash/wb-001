# -*- coding: utf-8 -*-
"""仓库资产检查：HTML 原型、Markdown 链接、SVG 合法性。

本地与 GitHub Actions 共用同一入口：
    python scripts/check_assets.py

检查项：
1. prototype 下的 HTML 能被解析、标签闭合，内部锚点链接能找到对应 id；
2. README、业务规格说明书、AGENTS.md 中的相对链接和图片路径真实存在；
3. diagrams 下的 SVG 能被解析为合法 XML。

任何一项失败都以非零退出码结束，不做忽略或跳过。
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# HTML 中无需闭合的标签
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# HTML 规范允许省略结束标签的元素：遇到这些新标签时，自动关闭同类的未闭合元素
AUTO_CLOSE_ON_START = {
    "li": {"li"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "option": {"option"},
    "optgroup": {"option", "optgroup"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "thead": {"tr", "td", "th", "thead", "tbody", "tfoot"},
    "tbody": {"tr", "td", "th", "thead", "tbody", "tfoot"},
    "tfoot": {"tr", "td", "th", "thead", "tbody", "tfoot"},
    "p": {"p"},
    "a": {"a"},
}

# 允许省略结束标签的元素：当它们被父级结束标签顺带关闭时不算错误
OPTIONAL_END_TAGS = {
    "li", "dt", "dd", "p", "option", "optgroup", "tr", "td", "th",
    "thead", "tbody", "tfoot", "colgroup", "caption", "rt", "rp",
}

# 会隐式结束 <p> 的块级标签
P_CLOSERS = {
    "address", "article", "aside", "blockquote", "div", "dl", "fieldset",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "main", "nav", "ol", "p", "pre", "section", "table", "ul",
}

# 原型脚本用 location.hash 做前端路由：#home 对应 id="page-home"。
# 因此锚点校验允许 #x 命中 id="x" 或 id="page-x" 两种写法。
PAGE_ID_PREFIX = "page-"

MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "javascript:", "data:")


class HtmlChecker(HTMLParser):
    """按标签栈检查 HTML 是否闭合，并收集 id 与链接。"""

    def __init__(self, path: Path) -> None:
        super().__init__(convert_charrefs=True)
        self.path = path
        self.stack: list[tuple[str, int]] = []
        self.ids: dict[str, int] = {}
        self.errors: list[str] = []
        self.anchors: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]

        # 按规范自动结束同类的可省略标签
        closers = AUTO_CLOSE_ON_START.get(tag)
        if closers and self.stack and self.stack[-1][0] in closers:
            self.stack.pop()
        elif tag in P_CLOSERS and self.stack and self.stack[-1][0] == "p":
            self.stack.pop()

        attr_map = dict(attrs)
        if attr_map.get("id"):
            self.ids[attr_map["id"]] = line
        href = attr_map.get("href")
        if href is not None:
            self.anchors.append((href, line))

        if tag not in VOID_TAGS and not self.get_starttag_text().rstrip().endswith("/>"):
            self.stack.append((tag, line))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <x /> 表示开始即结束，只记录属性，不入栈
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        line = self.getpos()[0]
        if tag in VOID_TAGS:
            self.errors.append(f"{self.path.name}:{line} 多余的自闭合标签 </{tag}>")
            return
        if not self.stack:
            self.errors.append(f"{self.path.name}:{line} 出现无对应开始标签的 </{tag}>")
            return
        if self.stack[-1][0] == tag:
            self.stack.pop()
            return
        # 向下查找可匹配的开始标签
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                for name, open_line in self.stack[index + 1:]:
                    if name not in OPTIONAL_END_TAGS:
                        self.errors.append(
                            f"{self.path.name}:{line} 结束 </{tag}> 时 <{name}>（第 {open_line} 行）仍未闭合"
                        )
                del self.stack[index:]
                return
        self.errors.append(f"{self.path.name}:{line} 出现无对应开始标签的 </{tag}>")

    def close(self) -> None:
        super().close()
        for name, line in self.stack:
            if name not in OPTIONAL_END_TAGS:
                self.errors.append(f"{self.path.name}:{line} <{name}> 到文件结束仍未闭合")


def check_html() -> list[str]:
    problems: list[str] = []
    for path in sorted((ROOT / "prototype").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        checker = HtmlChecker(path)
        checker.feed(text)
        checker.close()
        problems.extend(checker.errors)

        for href, line in checker.anchors:
            if href.startswith("#"):
                target = urllib.parse.unquote(href[1:])
                if not target:
                    continue
                if target in checker.ids or PAGE_ID_PREFIX + target in checker.ids:
                    continue
                problems.append(
                    f"{path.name}:{line} 锚点 {href} 找不到 id=\"{target}\" 或 id=\"{PAGE_ID_PREFIX}{target}\""
                )
            elif not href.startswith(SKIP_PREFIXES) and not href.startswith("//"):
                # 相对文件链接（例如 index.html 指向 pc.html）
                target = urllib.parse.unquote(href.split("#")[0].split("?")[0])
                if not target:
                    continue
                if not (path.parent / target).exists():
                    problems.append(f"{path.name}:{line} 相对链接 {href} 指向的文件不存在")
    return problems


def markdown_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "unique-业务规格说明书.md"]
    files.extend(sorted((ROOT / "docs").glob("*.md")) if (ROOT / "docs").is_dir() else [])
    return [p for p in files if p.is_file()]


def check_markdown() -> list[str]:
    problems: list[str] = []
    for path in markdown_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            for match in MD_LINK_RE.finditer(line):
                raw = match.group(1).strip()
                if raw.startswith("<") and raw.endswith(">"):
                    raw = raw[1:-1].strip()
                if not raw or raw.startswith(SKIP_PREFIXES) or raw.startswith("#"):
                    continue
                target = urllib.parse.unquote(raw.split("#")[0].split("?")[0])
                if not target:
                    continue
                resolved = (path.parent / target).resolve()
                if not resolved.exists():
                    problems.append(f"{path.name}:{number} 链接 {raw} 指向的路径不存在")
    return problems


def check_svg() -> list[str]:
    problems: list[str] = []
    for path in sorted((ROOT / "diagrams").glob("*.svg")):
        try:
            ET.parse(path)
        except ET.ParseError as error:
            problems.append(f"{path.name} 不是合法 XML：{error}")
    return problems


def main() -> int:
    groups = [
        ("HTML 原型", check_html()),
        ("Markdown 链接", check_markdown()),
        ("SVG 图形", check_svg()),
    ]

    failed = False
    for name, problems in groups:
        print(f"== {name} ==")
        if problems:
            failed = True
            for item in problems:
                print(f"  [失败] {item}")
        else:
            print("  [通过]")

    print()
    print("检查结果：存在待修复问题" if failed else "检查结果：全部通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
