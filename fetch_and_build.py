#!/usr/bin/env python3
"""公開RSSフィードから最新情報を取得し、静的な dist/index.html を生成するスクリプト。

使い方:
    pip install -r requirements.txt
    python fetch_and_build.py

環境変数（任意）:
    AFFILIATE_ID      : アフィリエイトID（未設定時はプレースホルダー YOUR_AFFILIATE_ID）
    AFFILIATE_PARAM   : リンクに付与するクエリパラメータ名（既定: aff_id）
    MAX_ITEMS_PER_FEED: フィードごとの最大取得件数（既定: 8）
"""

from __future__ import annotations

import html
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

FEEDS: list[dict[str, str]] = [
    {"name": "ITmedia NEWS", "category": "テック", "url": "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml"},
    {"name": "GIGAZINE", "category": "テック", "url": "https://gigazine.net/news/rss_2.0/"},
    {"name": "Publickey", "category": "テック", "url": "https://www.publickey1.jp/atom.xml"},
    {"name": "Hacker News", "category": "テック", "url": "https://hnrss.org/frontpage"},
    {"name": "ギズモード・ジャパン", "category": "ガジェット/セール", "url": "https://www.gizmodo.jp/index.xml"},
    {"name": "ITmedia Mobile", "category": "ガジェット/セール", "url": "https://rss.itmedia.co.jp/rss/2.0/mobile.xml"},
]

SITE_TITLE = "Auto Deal Tracker"
SITE_DESCRIPTION = "テックニュースとセール速報を毎朝7時に自動でまとめてお届け"
AFFILIATE_ID = os.environ.get("AFFILIATE_ID", "YOUR_AFFILIATE_ID")
AFFILIATE_PARAM = os.environ.get("AFFILIATE_PARAM", "aff_id")
MAX_ITEMS_PER_FEED = int(os.environ.get("MAX_ITEMS_PER_FEED", "8"))
SUMMARY_LENGTH = 120
REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; AutoDealTracker/1.0; +https://github.com/)"

JST = timezone(timedelta(hours=9))
OUTPUT_PATH = Path(__file__).resolve().parent / "dist" / "index.html"


# ---------------------------------------------------------------------------
# データ取得・整形
# ---------------------------------------------------------------------------

@dataclass
class Item:
    title: str
    summary: str
    link: str
    published: datetime | None
    source: str
    category: str


def strip_html(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, length: int = SUMMARY_LENGTH) -> str:
    return text if len(text) <= length else text[:length].rstrip() + "…"


def parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc).astimezone(JST)
            except (TypeError, ValueError):
                continue
    return None


def build_affiliate_link(url: str) -> str:
    """リンクにアフィリエイト用のクエリパラメータ（プレースホルダー）を付与する。"""
    if not url:
        return "#"
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[AFFILIATE_PARAM] = AFFILIATE_ID
    return urlunparse(parts._replace(query=urlencode(query)))


def fetch_feed(feed: dict[str, str]) -> list[Item]:
    try:
        resp = requests.get(feed["url"], headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[WARN] {feed['name']}: 取得失敗 ({exc})", file=sys.stderr)
        return []

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        print(f"[WARN] {feed['name']}: パース失敗 ({parsed.bozo_exception})", file=sys.stderr)
        return []

    items: list[Item] = []
    for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
        title = strip_html(entry.get("title", "")) or "(無題)"
        raw_summary = entry.get("summary") or entry.get("description") or ""
        if not raw_summary and entry.get("content"):
            raw_summary = entry["content"][0].get("value", "")
        items.append(
            Item(
                title=title,
                summary=truncate(strip_html(raw_summary)) or "要約はありません。",
                link=entry.get("link", ""),
                published=parse_date(entry),
                source=feed["name"],
                category=feed["category"],
            )
        )
    print(f"[INFO] {feed['name']}: {len(items)} 件取得")
    return items


def collect_items() -> list[Item]:
    items: list[Item] = []
    seen: set[str] = set()
    for feed in FEEDS:
        for item in fetch_feed(feed):
            key = item.link or item.title
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    oldest = datetime.min.replace(tzinfo=JST)
    items.sort(key=lambda i: i.published or oldest, reverse=True)
    return items


# ---------------------------------------------------------------------------
# HTML 生成
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #f5f7fb; --surface: #ffffff; --text: #1d2433; --muted: #5d6b82;
  --border: #e3e8f0; --accent: #2563eb; --accent-hover: #1d4ed8;
  --tag-tech: #e0ecff; --tag-tech-text: #1e40af;
  --tag-deal: #ffe9d6; --tag-deal-text: #9a3412;
  --shadow: 0 1px 3px rgba(16,24,40,.06), 0 4px 12px rgba(16,24,40,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f141c; --surface: #18202b; --text: #e6ebf2; --muted: #9aa6b8;
    --border: #2a3442; --accent: #60a5fa; --accent-hover: #93c5fd;
    --tag-tech: #1e3a5f; --tag-tech-text: #bfdbfe;
    --tag-deal: #4a2a14; --tag-deal-text: #fed7aa;
    --shadow: 0 1px 3px rgba(0,0,0,.4);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text); line-height: 1.6;
  font-family: system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Noto Sans JP", Meiryo, sans-serif;
}
.container { max-width: 1200px; margin: 0 auto; padding: 0 16px; }
header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 28px 0 20px; }
header h1 { margin: 0; font-size: clamp(1.5rem, 4vw, 2.1rem); letter-spacing: -.01em; }
header p { margin: 6px 0 0; color: var(--muted); }
.meta { margin-top: 10px; font-size: .85rem; color: var(--muted); }
.filters { display: flex; flex-wrap: wrap; gap: 8px; margin: 24px 0 8px; }
.filters button {
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
  padding: 6px 14px; border-radius: 999px; font: inherit; font-size: .9rem; cursor: pointer;
}
.filters button[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; }
.grid {
  display: grid; gap: 18px; margin: 16px 0 40px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr));
}
.card {
  display: flex; flex-direction: column; background: var(--surface);
  border: 1px solid var(--border); border-radius: 12px; padding: 18px; box-shadow: var(--shadow);
  transition: transform .15s ease, box-shadow .15s ease;
}
.card:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(16,24,40,.12); }
.card-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: .78rem; color: var(--muted); }
.tag { padding: 2px 8px; border-radius: 6px; font-weight: 600; background: var(--tag-tech); color: var(--tag-tech-text); }
.tag.deal { background: var(--tag-deal); color: var(--tag-deal-text); }
.card h2 { font-size: 1.05rem; line-height: 1.45; margin: 10px 0 8px; overflow-wrap: anywhere; }
.card p { margin: 0 0 16px; color: var(--muted); font-size: .9rem; flex: 1; overflow-wrap: anywhere; }
.card time { margin-left: auto; }
.btn {
  align-self: flex-start; text-decoration: none; background: var(--accent); color: #fff;
  padding: 8px 16px; border-radius: 8px; font-size: .88rem; font-weight: 600;
}
.btn:hover { background: var(--accent-hover); }
.empty { text-align: center; color: var(--muted); padding: 60px 0; }
footer { border-top: 1px solid var(--border); padding: 20px 0 32px; font-size: .8rem; color: var(--muted); }
"""

JS = """
document.querySelectorAll('.filters button').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var cat = btn.dataset.filter;
    document.querySelectorAll('.filters button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b === btn));
    });
    document.querySelectorAll('.card').forEach(function (card) {
      card.hidden = cat !== 'all' && card.dataset.category !== cat;
    });
  });
});
"""


def render_card(item: Item) -> str:
    e = html.escape
    date_html = ""
    if item.published:
        date_html = (
            f'<time datetime="{item.published.isoformat()}">'
            f'{item.published.strftime("%Y/%m/%d %H:%M")}</time>'
        )
    tag_class = "tag deal" if "セール" in item.category else "tag"
    return f"""
      <article class="card" data-category="{e(item.category)}">
        <div class="card-head">
          <span class="{tag_class}">{e(item.category)}</span>
          <span>{e(item.source)}</span>
          {date_html}
        </div>
        <h2>{e(item.title)}</h2>
        <p>{e(item.summary)}</p>
        <a class="btn" href="{e(build_affiliate_link(item.link))}" target="_blank" rel="noopener sponsored nofollow">続きを読む →</a>
      </article>"""


def render_page(items: list[Item]) -> str:
    e = html.escape
    now = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")
    categories = sorted({i.category for i in items})
    filter_buttons = '<button type="button" data-filter="all" aria-pressed="true">すべて</button>' + "".join(
        f'<button type="button" data-filter="{e(c)}" aria-pressed="false">{e(c)}</button>' for c in categories
    )
    cards = "".join(render_card(i) for i in items) or '<p class="empty">現在表示できる記事がありません。</p>'
    sources = "、".join(e(f["name"]) for f in FEEDS)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{e(SITE_DESCRIPTION)}">
  <title>{e(SITE_TITLE)}</title>
  <style>{CSS}</style>
</head>
<body>
  <header>
    <div class="container">
      <h1>{e(SITE_TITLE)}</h1>
      <p>{e(SITE_DESCRIPTION)}</p>
      <div class="meta">最終更新: {now} (JST) ・ {len(items)} 件</div>
    </div>
  </header>
  <main class="container">
    <nav class="filters" aria-label="カテゴリ絞り込み">{filter_buttons}</nav>
    <section class="grid">{cards}
    </section>
  </main>
  <footer>
    <div class="container">
      <p>情報元: {sources}。各記事の著作権は配信元に帰属します。</p>
      <p>当サイトはアフィリエイト広告（Amazonアソシエイト含む）を掲載しています。Amazonのアソシエイトとして、当メディアは適格販売により収入を得ています。</p>
    </div>
  </footer>
  <script>{JS}</script>
</body>
</html>
"""


def main() -> int:
    items = collect_items()
    if not items:
        print("[ERROR] 1件も取得できませんでした。既存の index.html は更新しません。", file=sys.stderr)
        return 1
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render_page(items), encoding="utf-8")
    print(f"[INFO] {OUTPUT_PATH} を生成しました（{len(items)} 件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
