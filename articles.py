"""独自記事（articles/*.md）の読み込みと Markdown → HTML 変換。

記事ファイルの書き方は articles/_template.md を参照。
- ファイル名（拡張子なし）がそのまま URL になる: articles/how-to-use.md → /articles/how-to-use/
- ファイル名は半角英小文字・数字・ハイフンのみ。先頭が "_" のファイルは公開しない（下書き・テンプレート用）
- date が未来の記事は、その日の自動更新まで公開しない（予約投稿）
- 本文中の楽天市場リンクは、もしもアフィリエイトのリンクに自動変換する
"""

from __future__ import annotations

import html
import re
import sys
import xml.etree.ElementTree as etree
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

from affiliate import (
    AFFILIATE_LINK_ATTRS,
    build_moshimo_rakuten_link,
    is_rakuten_ichiba_url,
    moshimo_rakuten_impression_url,
)

JST = timezone(timedelta(hours=9))
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


@dataclass
class Article:
    slug: str
    title: str
    date: date
    description: str
    body_html: str
    tags: list[str] = field(default_factory=list)
    updated: date | None = None
    image: str = ""
    has_affiliate: bool = False

    @property
    def path(self) -> str:
        """サイト内の URL パス（例: articles/how-to-use/）。"""
        return f"articles/{self.slug}/"

    @property
    def last_modified(self) -> date:
        return self.updated or self.date


# ---------------------------------------------------------------------------
# Markdown 変換（リンク加工つき）
# ---------------------------------------------------------------------------

class _LinkProcessor(Treeprocessor):
    """楽天市場リンクをアフィリエイトリンクに変換し、外部リンクは新しいタブで開く。"""

    def __init__(self, md, site_host: str):
        super().__init__(md)
        self.site_host = site_host

    def run(self, root):
        self.md.has_affiliate = False
        # 走査中に要素を挿入しないよう、先に (親, 子) の組を集める
        pairs = [(parent, child) for parent in root.iter() for child in parent]
        for parent, child in pairs:
            if child.tag == "img":
                child.set("loading", "lazy")
            if child.tag != "a":
                continue
            href = child.get("href", "")
            if is_rakuten_ichiba_url(href):
                child.set("href", build_moshimo_rakuten_link(href))
                for key, value in AFFILIATE_LINK_ATTRS.items():
                    child.set(key, value)
                pixel = etree.Element(
                    "img",
                    {
                        "class": "af-pixel",
                        "src": moshimo_rakuten_impression_url(),
                        "width": "1",
                        "height": "1",
                        "alt": "",
                        "loading": "lazy",
                    },
                )
                pixel.tail, child.tail = child.tail, None
                parent.insert(list(parent).index(child) + 1, pixel)
                self.md.has_affiliate = True
            elif href.startswith(("http://", "https://")) and urlparse(href).hostname != self.site_host:
                child.set("target", "_blank")
                child.set("rel", "noopener")


class _LinkExtension(Extension):
    def __init__(self, site_host: str):
        super().__init__()
        self.site_host = site_host

    def extendMarkdown(self, md):
        md.treeprocessors.register(_LinkProcessor(md, self.site_host), "affiliate_links", 5)


def render_markdown(text: str, site_host: str = "") -> tuple[str, bool]:
    """Markdown を HTML に変換し、(HTML, 楽天アフィリエイトリンクを含むか) を返す。"""
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "sane_lists", _LinkExtension(site_host)],
        output_format="html",
    )
    body = md.convert(text)
    return body, bool(getattr(md, "has_affiliate", False))


# ---------------------------------------------------------------------------
# 記事ファイルの読み込み
# ---------------------------------------------------------------------------

def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    match = FRONT_MATTER.match(raw.lstrip("﻿"))
    if not match:
        return {}, raw
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return meta, match.group(2)


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date() if value.strip() else None
    except ValueError:
        return None


def _plain_text(body_html: str, length: int = 120) -> str:
    text = re.sub(r"<[^>]+>", " ", body_html)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= length else text[:length].rstrip() + "…"


def load_article(path: Path, site_host: str = "", today: date | None = None) -> Article | None:
    """記事ファイルを1件読み込む。公開対象でなければ None を返す（理由はログに出す）。"""
    name = path.name
    slug = path.stem
    if slug.startswith("_"):
        return None
    if not SLUG_PATTERN.match(slug):
        print(f"[WARN] 記事 {name}: ファイル名は半角英小文字・数字・ハイフンのみ使えます（例: how-to-use.md）", file=sys.stderr)
        return None
    try:
        meta, body_md = _parse_front_matter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"[WARN] 記事 {name}: 読み込み失敗 ({exc})", file=sys.stderr)
        return None

    title = meta.get("title", "")
    published = _parse_date(meta.get("date", ""))
    if not title or not published:
        print(f"[WARN] 記事 {name}: title と date（YYYY-MM-DD）は必須です", file=sys.stderr)
        return None
    if meta.get("draft", "").lower() in ("true", "yes", "1"):
        print(f"[INFO] 記事 {name}: 下書き（draft: true）のため非公開")
        return None
    today = today or datetime.now(JST).date()
    if published > today:
        print(f"[INFO] 記事 {name}: 公開日 {published} 前のため非公開（予約投稿）")
        return None

    body_html, has_affiliate = render_markdown(body_md, site_host)
    image = meta.get("image", "")
    return Article(
        slug=slug,
        title=title,
        date=published,
        updated=_parse_date(meta.get("updated", "")),
        description=meta.get("description", "") or _plain_text(body_html),
        tags=[t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
        image=image if image.startswith(("https://", "/")) else "",
        body_html=body_html,
        has_affiliate=has_affiliate,
    )


def load_articles(directory: Path, site_host: str = "", today: date | None = None) -> list[Article]:
    """articles/ 以下の公開対象の記事を新しい順に返す。"""
    if not directory.is_dir():
        return []
    articles = [
        a for a in (load_article(p, site_host, today) for p in sorted(directory.glob("*.md"))) if a
    ]
    articles.sort(key=lambda a: (a.date, a.slug), reverse=True)
    return articles
