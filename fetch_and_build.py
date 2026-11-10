#!/usr/bin/env python3
"""公開RSSフィードから最新情報を取得し、静的な dist/index.html を生成するスクリプト。

使い方:
    pip install -r requirements.txt
    python fetch_and_build.py

環境変数（任意）:
    CONTACT_EMAIL     : お問い合わせ欄に表示するメールアドレス
    RAKUTEN_APP_ID    : 楽天ウェブサービスのアプリID（applicationId）
    RAKUTEN_ACCESS_KEY: 楽天ウェブサービスのアクセスキー（accessKey）
    RAKUTEN_SITE_URL  : 楽天アプリに登録した許可サイトURL（Referer/Origin として送信）
    ※ RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY が未設定の場合、楽天ランキング欄は表示しない。
    MAX_ITEMS_PER_FEED: フィードごとの最大取得件数（既定: 8）

アフィリエイト:
    リンク先が楽天市場（*.rakuten.co.jp）の場合のみ、もしもアフィリエイトの
    「どこでもリンク」形式に変換し、インプレッション計測用の 1x1 ピクセルを付与する。
    それ以外（ニュース記事など）は元の URL のままリンクする。
"""

from __future__ import annotations

import html
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import feedparser
import requests

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

FEEDS: list[dict[str, str]] = [
    {"name": "ITmedia NEWS", "category": "テック", "url": "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml"},
    {"name": "GIGAZINE", "category": "テック", "url": "https://gigazine.net/news/rss_2.0/"},
    {"name": "Publickey", "category": "テック", "url": "https://www.publickey1.jp/atom.xml"},
    {"name": "ギズモード・ジャパン", "category": "ガジェット/セール", "url": "https://www.gizmodo.jp/index.xml"},
    {"name": "ITmedia Mobile", "category": "ガジェット/セール", "url": "https://rss.itmedia.co.jp/rss/2.0/mobile.xml"},
    {"name": "PC Watch", "category": "ガジェット/セール", "url": "https://pc.watch.impress.co.jp/data/rss/1.0/pcw/feed.rdf"},
    {"name": "AKIBA PC Hotline!", "category": "ガジェット/セール", "url": "https://akiba-pc.watch.impress.co.jp/data/rss/1.0/ah/feed.rdf"},
    {"name": "GetNavi web", "category": "ガジェット/セール", "url": "https://getnavi.jp/feed/"},
]

SITE_TITLE = "Auto Deal Tracker"
SITE_DESCRIPTION = "テックニュースとセール速報を毎朝7時に自動でまとめてお届け"
SITE_ABOUT = (
    "Auto Deal Tracker は、主要テックメディアや公式発表から最新のガジェット情報・"
    "セール速報を自動収集・配信するキュレーションメディアです。"
)
OPERATOR_NAME = "Auto Deal Tracker 運営事務局"
# 連絡先メールアドレス。設定すると「お問い合わせ」欄に mailto リンクを表示する。
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
# もしもアフィリエイト × 楽天市場「どこでもリンク」の本番パラメータ
MOSHIMO_RAKUTEN_PARAMS = {"a_id": "5891942", "p_id": "54", "pc_id": "54", "pl_id": "616"}
MOSHIMO_CLICK_URL = "https://af.moshimo.com/af/c/click"
MOSHIMO_IMPRESSION_URL = "https://i.moshimo.com/af/i/impression"

# 楽天市場ランキングAPI（version: 2022-06-01）
RAKUTEN_RANKING_ENDPOINT = "https://openapi.rakuten.co.jp/ichibaranking/api/IchibaItem/Ranking/20220601"
RAKUTEN_APP_ID = os.environ.get("RAKUTEN_APP_ID", "")
RAKUTEN_ACCESS_KEY = os.environ.get("RAKUTEN_ACCESS_KEY", "")
RAKUTEN_SITE_URL = os.environ.get("RAKUTEN_SITE_URL", "")
RAKUTEN_GENRES: list[dict[str, str]] = [
    {"id": "100026", "name": "パソコン・周辺機器"},
    {"id": "564500", "name": "スマートフォン・タブレット"},
    {"id": "211742", "name": "TV・オーディオ・カメラ"},
    {"id": "562637", "name": "家電"},
]
RAKUTEN_ITEMS_PER_GENRE = 4
RAKUTEN_REQUEST_INTERVAL = 1.2  # 秒。短時間の連続アクセスを避ける
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


def is_rakuten_ichiba_url(url: str) -> bool:
    """楽天市場の URL か判定する（楽天の既存アフィリエイトリンク afl.rakuten.co.jp は除外）。"""
    host = (urlparse(url).hostname or "").lower()
    if not (host == "rakuten.co.jp" or host.endswith(".rakuten.co.jp")):
        return False
    return not (host == "afl.rakuten.co.jp" or host.endswith(".afl.rakuten.co.jp"))


def build_moshimo_rakuten_link(url: str) -> str:
    """楽天市場の URL を、もしもアフィリエイトの「どこでもリンク」に変換する。"""
    query = urlencode(MOSHIMO_RAKUTEN_PARAMS)
    return f"{MOSHIMO_CLICK_URL}?{query}&url={quote(url, safe='')}"


def moshimo_rakuten_impression_url() -> str:
    return f"{MOSHIMO_IMPRESSION_URL}?{urlencode(MOSHIMO_RAKUTEN_PARAMS)}"


def render_link(url: str, label: str) -> str:
    """カードのリンクボタンを生成する。楽天市場ならアフィリエイトリンク＋計測ピクセルを付与する。"""
    e = html.escape
    if not url:
        return ""
    if is_rakuten_ichiba_url(url):
        return (
            f'<a class="btn" href="{e(build_moshimo_rakuten_link(url))}" target="_blank" '
            f'rel="nofollow sponsored noopener" referrerpolicy="no-referrer-when-downgrade" '
            f'attributionsrc>{e(label)}</a>'
            f'<img class="af-pixel" src="{e(moshimo_rakuten_impression_url())}" '
            f'width="1" height="1" alt="" loading="lazy">'
        )
    return f'<a class="btn" href="{e(url)}" target="_blank" rel="noopener">{e(label)}</a>'


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


@dataclass
class Product:
    rank: int
    name: str
    price: int
    url: str
    image: str
    shop: str
    review_average: float
    review_count: int


def _first_image(value) -> str:
    """mediumImageUrls（文字列 or {"imageUrl": ...} の配列）から先頭画像を取り出し、拡大版にする。"""
    if not value:
        return ""
    first = value[0]
    url = first.get("imageUrl", "") if isinstance(first, dict) else str(first)
    return re.sub(r"_ex=\d+x\d+", "_ex=300x300", url)


def fetch_rakuten_ranking(genre_id: str) -> list[Product]:
    params = {
        "applicationId": RAKUTEN_APP_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "genreId": genre_id,
        "format": "json",
        "formatVersion": 2,
        "elements": "rank,itemName,itemPrice,itemUrl,mediumImageUrls,shopName,reviewAverage,reviewCount",
    }
    headers = {"User-Agent": USER_AGENT}
    if RAKUTEN_SITE_URL:
        headers["Referer"] = RAKUTEN_SITE_URL
        parsed = urlparse(RAKUTEN_SITE_URL)
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    try:
        resp = requests.get(RAKUTEN_RANKING_ENDPOINT, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"[WARN] 楽天ランキング({genre_id}): 通信失敗 ({type(exc).__name__})", file=sys.stderr)
        return []
    if resp.status_code != 200:
        # エラー本文には認証情報は含まれないため、原因調査用にそのまま出力する
        print(f"[WARN] 楽天ランキング({genre_id}): HTTP {resp.status_code} {resp.text[:300]}", file=sys.stderr)
        return []

    try:
        data = resp.json()
    except ValueError:
        print(f"[WARN] 楽天ランキング({genre_id}): JSON以外の応答", file=sys.stderr)
        return []

    products: list[Product] = []
    for raw in data.get("Items", data.get("items", [])):
        item = raw.get("Item", raw) if isinstance(raw, dict) else {}
        url = item.get("itemUrl", "")
        if not url:
            continue
        products.append(
            Product(
                rank=int(item.get("rank") or 0),
                name=strip_html(item.get("itemName", "")),
                price=int(item.get("itemPrice") or 0),
                url=url,
                image=_first_image(item.get("mediumImageUrls")),
                shop=item.get("shopName", ""),
                review_average=float(item.get("reviewAverage") or 0),
                review_count=int(item.get("reviewCount") or 0),
            )
        )
        if len(products) >= RAKUTEN_ITEMS_PER_GENRE:
            break
    return products


def collect_rakuten_rankings() -> list[tuple[str, list[Product]]]:
    if not (RAKUTEN_APP_ID and RAKUTEN_ACCESS_KEY):
        print("[INFO] 楽天APIのキーが未設定のため、ランキング欄をスキップします")
        return []
    sections: list[tuple[str, list[Product]]] = []
    for i, genre in enumerate(RAKUTEN_GENRES):
        if i:
            time.sleep(RAKUTEN_REQUEST_INTERVAL)
        products = fetch_rakuten_ranking(genre["id"])
        print(f"[INFO] 楽天ランキング {genre['name']}: {len(products)} 件取得")
        if products:
            sections.append((genre["name"], products))
    return sections


# ---------------------------------------------------------------------------
# HTML 生成
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #f5f7fb; --surface: #ffffff; --text: #1d2433; --muted: #5d6b82;
  --border: #e3e8f0; --accent: #2563eb; --accent-hover: #1d4ed8;
  --tag-tech: #e0ecff; --tag-tech-text: #1e40af;
  --tag-deal: #ffe9d6; --tag-deal-text: #9a3412;
  --about-bg: #eef4ff;
  --shadow: 0 1px 3px rgba(16,24,40,.06), 0 4px 12px rgba(16,24,40,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f141c; --surface: #18202b; --text: #e6ebf2; --muted: #9aa6b8;
    --border: #2a3442; --accent: #60a5fa; --accent-hover: #93c5fd;
    --tag-tech: #1e3a5f; --tag-tech-text: #bfdbfe;
    --tag-deal: #4a2a14; --tag-deal-text: #fed7aa;
    --about-bg: #16233a;
    --shadow: 0 1px 3px rgba(0,0,0,.4);
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0; background: var(--bg); color: var(--text); line-height: 1.6;
  font-family: system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Noto Sans JP", Meiryo, sans-serif;
}
.container { max-width: 1200px; margin: 0 auto; padding: 0 16px; }
header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 28px 0 20px; }
header h1 { margin: 0; font-size: clamp(1.5rem, 4vw, 2.1rem); letter-spacing: -.01em; }
header p { margin: 6px 0 0; color: var(--muted); }
.meta { margin-top: 10px; font-size: .85rem; color: var(--muted); }
.about {
  margin: 16px 0 4px; padding: 14px 18px; max-width: 820px;
  background: var(--about-bg); border-left: 4px solid var(--accent); border-radius: 8px;
  font-size: .95rem; color: var(--text);
}
.about strong { color: var(--accent); }
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
.af-pixel { position: absolute; width: 1px; height: 1px; border: none; opacity: 0; pointer-events: none; }
.section-title { display: flex; align-items: center; gap: 10px; margin: 32px 0 4px; font-size: 1.25rem; }
.section-note { margin: 0 0 8px; font-size: .82rem; color: var(--muted); }
.pr {
  font-size: .7rem; font-weight: 700; padding: 2px 7px; border-radius: 4px;
  border: 1px solid var(--muted); color: var(--muted); letter-spacing: .05em;
}
.ranking { margin-bottom: 8px; }
.ranking h3 { margin: 18px 0 10px; font-size: .95rem; color: var(--text); }
.products {
  display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 200px), 1fr));
}
@media (max-width: 480px) { .products { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.product {
  position: relative; display: flex; flex-direction: column; background: var(--surface);
  border: 1px solid var(--border); border-radius: 12px; padding: 12px; box-shadow: var(--shadow);
}
.product .rank {
  position: absolute; top: 8px; left: 8px; min-width: 28px; height: 28px; padding: 0 6px;
  display: grid; place-items: center; border-radius: 999px; font-size: .8rem; font-weight: 700;
  background: var(--accent); color: #fff;
}
.product .thumb {
  aspect-ratio: 1; display: grid; place-items: center; background: #fff;
  border-radius: 8px; overflow: hidden; margin-bottom: 10px;
}
.product .thumb img { width: 100%; height: 100%; object-fit: contain; }
.product .name {
  margin: 0 0 6px; font-size: .85rem; line-height: 1.45; color: var(--text);
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.product .price { font-size: 1.05rem; font-weight: 700; color: var(--tag-deal-text); }
.product .sub { font-size: .75rem; color: var(--muted); margin: 2px 0 10px; overflow-wrap: anywhere; flex: 1; }
.product .btn { align-self: stretch; text-align: center; padding: 8px 10px; font-size: .82rem; background: #bf0000; }
.product .btn:hover { background: #990000; }
.empty { text-align: center; color: var(--muted); padding: 60px 0; }
footer {
  background: var(--surface); border-top: 1px solid var(--border);
  padding: 32px 0 24px; font-size: .82rem; color: var(--muted);
}
.footer-grid {
  display: grid; gap: 24px 32px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
}
.footer-grid section { min-width: 0; }
.footer-grid .policy { grid-column: span 2; }
@media (max-width: 600px) { .footer-grid .policy { grid-column: auto; } }
footer h3 {
  margin: 0 0 8px; font-size: .78rem; font-weight: 700; letter-spacing: .06em;
  text-transform: uppercase; color: var(--text);
}
footer p { margin: 0 0 6px; overflow-wrap: anywhere; }
footer ul { margin: 0; padding-left: 1.1em; }
footer li { margin-bottom: 6px; }
footer a { color: var(--accent); }
.footer-bottom {
  margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--border); font-size: .78rem;
}
.footer-bottom .disclosure { color: var(--text); }
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
        {render_link(item.link, "続きを読む →")}
      </article>"""


def render_product(p: Product) -> str:
    e = html.escape
    review = f"★{p.review_average:.1f}（{p.review_count:,}件）・" if p.review_count else ""
    image = (
        f'<img src="{e(p.image)}" alt="{e(p.name[:60])}" loading="lazy" width="300" height="300">'
        if p.image else ""
    )
    return f"""
          <article class="product">
            <span class="rank">{p.rank}</span>
            <div class="thumb">{image}</div>
            <p class="name">{e(p.name)}</p>
            <div class="price">¥{p.price:,}</div>
            <div class="sub">{review}{e(p.shop)}</div>
            {render_link(p.url, "楽天市場で見る")}
          </article>"""


def render_rankings(sections: list[tuple[str, list[Product]]]) -> str:
    if not sections:
        return ""
    e = html.escape
    blocks = "".join(
        f"""
      <h3>{e(name)}</h3>
      <div class="products">{"".join(render_product(p) for p in products)}
      </div>"""
        for name, products in sections
    )
    return f"""
    <section class="ranking" aria-labelledby="ranking-title">
      <h2 class="section-title" id="ranking-title">楽天市場 人気ランキング <span class="pr">PR</span></h2>
      <p class="section-note">楽天市場のジャンル別デイリーランキング上位を毎朝自動更新。価格・在庫は変動するため、購入前にリンク先でご確認ください。</p>{blocks}
    </section>
    <h2 class="section-title">最新ニュース・セール速報</h2>"""


def render_page(items: list[Item], rankings: list[tuple[str, list[Product]]] | None = None) -> str:
    e = html.escape
    now = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")
    categories = sorted({i.category for i in items})
    filter_buttons = '<button type="button" data-filter="all" aria-pressed="true">すべて</button>' + "".join(
        f'<button type="button" data-filter="{e(c)}" aria-pressed="false">{e(c)}</button>' for c in categories
    )
    cards = "".join(render_card(i) for i in items) or '<p class="empty">現在表示できる記事がありません。</p>'
    sources = "、".join(e(f["name"]) for f in FEEDS)
    if CONTACT_EMAIL:
        contact_html = (
            "<p>お気づきの点や掲載停止のご連絡は、下記までお問い合わせください。</p>"
            f'<p><a href="mailto:{e(CONTACT_EMAIL)}">{e(CONTACT_EMAIL)}</a></p>'
        )
    else:
        contact_html = "<p>お気づきの点や掲載停止のご連絡は、運営までお問い合わせください。</p>"
    year = datetime.now(JST).year
    about_html = e(SITE_ABOUT).replace(e(SITE_TITLE), "<strong>" + e(SITE_TITLE) + "</strong>", 1)

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
      <div class="about" role="note" aria-label="サイト概要">{about_html}</div>
      <div class="meta">最終更新: {now} (JST) ・ {len(items)} 件</div>
    </div>
  </header>
  <main class="container">{render_rankings(rankings or [])}
    <nav class="filters" aria-label="カテゴリ絞り込み">{filter_buttons}</nav>
    <section class="grid">{cards}
    </section>
  </main>
  <footer>
    <div class="container">
      <div class="footer-grid">
        <section>
          <h3>運営者情報</h3>
          <p>{e(OPERATOR_NAME)}</p>
        </section>
        <section>
          <h3>お問い合わせ</h3>
          {contact_html}
        </section>
        <section class="policy">
          <h3>プライバシーポリシー</h3>
          <ul>
            <li>当サイトでは、アクセス解析ツールおよび広告配信（Amazonアソシエイト、楽天アフィリエイト等のアフィリエイトプログラム）を利用しています。</li>
            <li>これらのサービスはCookieを使用してトラフィックデータやアクセス情報を収集する場合がありますが、個人を特定する情報は含みません。ブラウザの設定でCookieを無効にすることも可能です。</li>
          </ul>
        </section>
      </div>
      <div class="footer-bottom">
        <p class="disclosure">当サイトはアフィリエイト広告（Amazonアソシエイト含む）を掲載しています。Amazonのアソシエイトとして、当メディアは適格販売により収入を得ています。</p>
        <p>情報元: {sources}。各記事の著作権は配信元に帰属します。</p>
        <p>&copy; {year} {e(OPERATOR_NAME)}</p>
      </div>
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
    rankings = collect_rakuten_rankings()
    OUTPUT_PATH.write_text(render_page(items, rankings), encoding="utf-8")
    print(f"[INFO] {OUTPUT_PATH} を生成しました（{len(items)} 件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
