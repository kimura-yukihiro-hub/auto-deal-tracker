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
    「どこでもリンク」形式に変換し、インプレッション計測用の 1x1 ピクセルを付与する（affiliate.py）。
    それ以外（ニュース記事など）は元の URL のままリンクする。

独自記事:
    articles/*.md（Markdown）を dist/articles/<ファイル名>/index.html に変換する（articles.py）。
    書き方は articles/_template.md を参照。
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from email.utils import format_datetime
from urllib.parse import quote, urlparse

import feedparser
import requests

from affiliate import build_moshimo_rakuten_link, is_rakuten_ichiba_url, moshimo_rakuten_impression_url
from articles import Article, load_articles
from digest import is_sale_title, maybe_write_weekly_digest, update_log

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

FEEDS: list[dict[str, str]] = [
    # 読者像: スマホ・パソコン・家電を賢く買いたい人（詳しくない人・仕事で PC を使う人）
    # 2026-11-11 見直し: GIGAZINE（話題が幅広すぎる）・Publickey（開発者向け）・AKIBA PC Hotline!（自作 PC 向け）を除外
    {"name": "ITmedia NEWS", "category": "IT・ネット", "url": "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml"},
    {"name": "ギズモード・ジャパン", "category": "製品ニュース", "url": "https://www.gizmodo.jp/index.xml"},
    {"name": "ITmedia Mobile", "category": "製品ニュース", "url": "https://rss.itmedia.co.jp/rss/2.0/mobile.xml"},
    {"name": "PC Watch", "category": "製品ニュース", "url": "https://pc.watch.impress.co.jp/data/rss/1.0/pcw/feed.rdf"},
    # GetNavi web は家電・デジタル製品のジャンルだけ取り込む（グルメ・ファッション等は除外）
    {"name": "GetNavi web", "category": "製品ニュース", "url": "https://getnavi.jp/feed/",
     "include": r"^https://getnavi\.jp/(?:gadgets|appliances)/"},
]

SITE_TITLE = "セールの見張り番"
# 公開URL（末尾スラッシュ付き）。独自ドメインに移行したら環境変数 SITE_URL で上書きする
SITE_URL = os.environ.get("SITE_URL", "").strip() or "https://auto-deal-tracker.pages.dev/"
if not SITE_URL.endswith("/"):
    SITE_URL += "/"
# Google Search Console の所有権確認用（公開して問題ない値）。削除すると確認が外れるので残しておく
GOOGLE_SITE_VERIFICATION = "PEJzJAjl6FvLmRWf8WOBif-XcYhrC2KI0lsWLUL-MCM"
# Bing Webmaster Tools の所有権確認用（同上）
BING_SITE_VERIFICATION = "3F76F98F782898A2AFB06DB65D9B5FEC"
# IndexNow（Bing 等へ更新を通知）の所有確認キー。dist/{キー}.txt として公開する（公開して問題ない値）
INDEXNOW_KEY = "c4cf435d8df235b2e9df3d60fb44feb3"
OGP_IMAGE = "ogp.png"  # dist/ogp.png（assets/ogp-template.html から作成した静的画像）
SITE_DESCRIPTION = "スマホ・パソコン・家電のセールと、買い物に役立つ最新ニュースを毎朝チェック"
SITE_ABOUT = (
    "セールの見張り番 は、スマホ・パソコン・家電を少しでもお得に買いたい人のために、"
    "主要IT・家電メディアのセール情報と買い物に役立つニュースを毎朝自動で集め、"
    "編集部のおすすめとあわせてお届けするサイトです。"
)
OPERATOR_NAME = "セールの見張り番 運営事務局"
# 連絡先メールアドレス。設定すると「お問い合わせ」欄に mailto リンクを表示する。
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
# お問い合わせフォーム（Google フォームなど）の公開 URL。設定するとフッターにボタンを表示する
CONTACT_FORM_URL = "https://forms.gle/6e4YNCg8x5oGChBY6"

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
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = BASE_DIR / "dist" / "index.html"

# 編集部ピックアップ（data/featured.json）。空・未設定なら自動ピックアップに切り替える
FEATURED_PATH = BASE_DIR / "data" / "featured.json"
MAX_FEATURED = 6
FALLBACK_PICK_COUNT = 3
FALLBACK_PICK_CATEGORY = "製品ニュース"

# 独自記事（articles/*.md → dist/articles/<slug>/index.html）
ARTICLES_DIR = BASE_DIR / "articles"
TOP_ARTICLE_COUNT = 3  # トップページに表示する最新記事の数
DEALS_LOG_PATH = BASE_DIR / "data" / "deals_log.json"  # 週刊まとめ用のセール記事の記録（digest.py）


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
        gh_annotate("warning", "ニュース取得", f"{feed['name']}: 取得失敗（{type(exc).__name__}）{feed['url']}")
        return []

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        print(f"[WARN] {feed['name']}: パース失敗 ({parsed.bozo_exception})", file=sys.stderr)
        gh_annotate("warning", "ニュース取得", f"{feed['name']}: RSS を読み取れません（URL が変わった可能性）{feed['url']}")
        return []

    items: list[Item] = []
    entries = parsed.entries
    if feed.get("include"):
        entries = [en for en in entries if re.search(feed["include"], en.get("link", ""))]
    for entry in entries[:MAX_ITEMS_PER_FEED]:
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
    if not items:
        gh_annotate("warning", "ニュース取得", f"{feed['name']}: 記事が 0 件でした（RSS の形式が変わった可能性）{feed['url']}")
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
class Pick:
    title: str
    comment: str
    url: str
    manual: bool  # True: featured.json の手動登録 / False: 自動ピックアップ
    image: str = ""
    price: str = ""
    label: str = ""
    source: str = ""
    date: str = ""


def _format_price(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"¥{int(value):,}"
    return str(value or "").strip()


def load_featured(path: Path | None = None) -> list[Pick]:
    """data/featured.json から手動ピックアップを読み込む。不備があっても例外にせず空を返す。"""
    path = path or FEATURED_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "null")
    except (OSError, ValueError) as exc:
        print(f"[WARN] {path.name} を読み込めません。自動ピックアップに切り替えます ({exc})", file=sys.stderr)
        return []
    entries = data.get("items", []) if isinstance(data, dict) else data if isinstance(data, list) else []

    today = datetime.now(JST).date()
    picks: list[Pick] = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title", "")).strip()
        url = str(entry.get("url", "")).strip()
        if not title or not url.startswith(("https://", "http://")):
            print(f"[WARN] {path.name} の {i} 件目: title と url（http/https）が必須のためスキップ", file=sys.stderr)
            continue
        expires = str(entry.get("expires", "")).strip()
        if expires:
            try:
                if datetime.strptime(expires, "%Y-%m-%d").date() < today:
                    print(f"[INFO] {path.name} の「{title}」は掲載期限 {expires} を過ぎたため非表示")
                    continue
            except ValueError:
                print(f"[WARN] {path.name} の {i} 件目: expires は YYYY-MM-DD 形式で指定してください", file=sys.stderr)
        image = str(entry.get("image", "")).strip()
        picks.append(
            Pick(
                title=title,
                comment=str(entry.get("comment", "")).strip(),
                url=url,
                manual=True,
                image=image if image.startswith("https://") else "",
                price=_format_price(entry.get("price")),
                label=str(entry.get("label", "")).strip() or "編集部おすすめ",
            )
        )
    return picks[:MAX_FEATURED]


def select_picks(items: list[Item]) -> list[Pick]:
    """手動ピックアップがあれば優先し、なければセール記事（足りなければ製品ニュース）の最新から自動で選ぶ。"""
    manual = load_featured()
    if manual:
        print(f"[INFO] 編集部ピックアップ: featured.json から {len(manual)} 件")
        return manual
    candidates = [i for i in items if i.link and is_sale_title(i.title)]
    candidates += [i for i in items if i.link and i.category == FALLBACK_PICK_CATEGORY and i not in candidates]
    auto = [
        Pick(
            title=item.title,
            comment=item.summary,
            url=item.link,
            manual=False,
            label="自動ピックアップ",
            source=item.source,
            date=item.published.strftime("%m/%d %H:%M") if item.published else "",
        )
        for item in candidates
    ][:FALLBACK_PICK_COUNT]
    print(f"[INFO] 編集部ピックアップ: featured.json が空のため自動で {len(auto)} 件")
    return auto


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


def gh_annotate(level: str, title: str, message: str) -> None:
    """GitHub Actions 上なら、実行画面に注釈（notice / warning）として表示する。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    msg = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title={title}::{msg}")


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
        msg = f"ジャンル {genre_id}: 通信失敗 ({type(exc).__name__})"
        print(f"[WARN] 楽天ランキング {msg}", file=sys.stderr)
        gh_annotate("warning", "楽天ランキング", msg)
        return []
    if resp.status_code != 200:
        # エラー本文には認証情報は含まれないため、原因調査用にそのまま出力する
        msg = f"ジャンル {genre_id}: HTTP {resp.status_code} {resp.text[:300]}"
        print(f"[WARN] 楽天ランキング {msg}", file=sys.stderr)
        gh_annotate("warning", "楽天ランキング", msg)
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
    status = (
        f"RAKUTEN_APP_ID: {'設定あり' if RAKUTEN_APP_ID else '未設定'} / "
        f"RAKUTEN_ACCESS_KEY: {'設定あり' if RAKUTEN_ACCESS_KEY else '未設定'} / "
        f"RAKUTEN_SITE_URL: {'設定あり' if RAKUTEN_SITE_URL else '未設定'}"
    )
    if not (RAKUTEN_APP_ID and RAKUTEN_ACCESS_KEY):
        print(f"[INFO] 楽天APIのキーが未設定のため、ランキング欄をスキップします（{status}）")
        gh_annotate("notice", "楽天ランキング", f"キー未設定のためスキップ（{status}）")
        return []
    sections: list[tuple[str, list[Product]]] = []
    for i, genre in enumerate(RAKUTEN_GENRES):
        if i:
            time.sleep(RAKUTEN_REQUEST_INTERVAL)
        products = fetch_rakuten_ranking(genre["id"])
        print(f"[INFO] 楽天ランキング {genre['name']}: {len(products)} 件取得")
        if products:
            sections.append((genre["name"], products))
    total = sum(len(p) for _, p in sections)
    gh_annotate(
        "notice" if total else "warning",
        "楽天ランキング",
        f"{len(sections)}/{len(RAKUTEN_GENRES)} ジャンル、{total} 件取得（{status}）",
    )
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
.filters {
  display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 8px; padding: 10px 0;
  position: sticky; top: 0; z-index: 5; background: var(--bg);
}
.top-nav { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 4px 18px; font-size: .85rem; margin-bottom: 6px; }
.top-nav a { color: var(--muted); text-decoration: none; }
.top-nav a:hover { color: var(--accent); text-decoration: underline; }
#sale, #news { scroll-margin-top: 12px; }
.sales { margin-top: 8px; }
.sale-items {
  display: grid; gap: 12px; margin-top: 12px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr));
}
.sale-item {
  background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--tag-deal-text);
  border-radius: 10px; padding: 12px 14px; box-shadow: var(--shadow); min-width: 0;
}
.sale-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: .75rem; color: var(--muted); }
.sale-meta time { margin-left: auto; }
.sale-item h3 { margin: 6px 0 4px; font-size: .95rem; line-height: 1.5; overflow-wrap: anywhere; }
.sale-item h3 a, .card h3 a { color: var(--text); text-decoration: none; }
.sale-item h3 a:hover, .card h3 a:hover { color: var(--accent); text-decoration: underline; }
.sale-item p { margin: 0; font-size: .8rem; color: var(--muted); overflow-wrap: anywhere; }
.more-wrap { text-align: center; margin: -16px 0 40px; }
.sale-footer { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px 16px; margin: 12px 0 0; }
.more-link-inline { color: var(--accent); font-size: .9rem; }
.share { display: flex; flex-wrap: wrap; gap: 8px; }
.share-btn {
  display: inline-flex; align-items: center; padding: 7px 14px; border-radius: 999px;
  font-size: .82rem; font-weight: 700; text-decoration: none; color: #fff !important;
}
.share-x { background: #111; }
.share-line { background: #06c755; }
.share-hatena { background: #00a4de; }
.share-btn:hover { opacity: .85; }
.post-share { margin-top: 32px; padding-top: 20px; border-top: 1px solid var(--border); }
.post-share p { margin: 0 0 10px; font-size: .85rem; color: var(--muted); }
.post-share .follow-note { margin: 14px 0 0; }
.follow-x {
  display: inline-block; padding: 6px 14px; border-radius: 999px; background: #111;
  color: #fff !important; text-decoration: none; font-weight: 700; font-size: .8rem;
}
.follow-x:hover { opacity: .85; }
.more-btn {
  font: inherit; font-size: .95rem; font-weight: 600; padding: 10px 28px; border-radius: 999px; cursor: pointer;
  background: var(--surface); color: var(--accent); border: 1px solid var(--accent);
}
.more-btn:hover { background: var(--accent); color: #fff; }
.filters button {
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
  padding: 6px 14px; border-radius: 999px; font: inherit; font-size: .9rem; cursor: pointer;
}
.filters button[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; }
.filters .count { font-size: .75em; opacity: .75; margin-left: 2px; }
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
.card h3 { font-size: 1.05rem; line-height: 1.45; margin: 10px 0 8px; overflow-wrap: anywhere; }
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
.pickup { margin-top: 8px; }
.pickups {
  display: grid; gap: 16px; margin-top: 12px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
}
.pick {
  position: relative; display: flex; gap: 14px; background: var(--surface);
  border: 1px solid var(--border); border-top: 4px solid var(--accent);
  border-radius: 12px; padding: 16px; box-shadow: var(--shadow);
}
.pick.manual { border-top-color: #bf0000; }
.pick .pick-img {
  flex: 0 0 96px; height: 96px; border-radius: 8px; overflow: hidden; background: #fff;
  display: grid; place-items: center;
}
.pick .pick-img img { width: 100%; height: 100%; object-fit: contain; }
.pick .pick-body { display: flex; flex-direction: column; min-width: 0; flex: 1; }
.pick .pick-label { font-size: .72rem; font-weight: 700; color: var(--accent); }
.pick.manual .pick-label { color: #bf0000; }
.pick h3 { margin: 4px 0 6px; font-size: 1rem; line-height: 1.45; overflow-wrap: anywhere; }
.pick p { margin: 0 0 8px; font-size: .85rem; color: var(--muted); overflow-wrap: anywhere; flex: 1; }
.pick .pick-meta { font-size: .75rem; color: var(--muted); margin-bottom: 10px; }
.pick .pick-price { font-size: 1.05rem; font-weight: 700; color: var(--tag-deal-text); margin-bottom: 10px; }
.pick.manual .btn { background: #bf0000; }
.pick.manual .btn:hover { background: #990000; }
@media (max-width: 480px) { .pick .pick-img { flex-basis: 72px; height: 72px; } }
.empty { text-align: center; color: var(--muted); padding: 60px 0; }
/* 独自記事 */
.article-cards {
  display: grid; gap: 16px; margin-top: 12px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr));
}
.article-card {
  display: flex; flex-direction: column; background: var(--surface);
  border: 1px solid var(--border); border-left: 4px solid var(--accent);
  border-radius: 12px; padding: 16px 18px; box-shadow: var(--shadow);
}
.article-card h3 { margin: 8px 0 6px; font-size: 1.02rem; line-height: 1.5; overflow-wrap: anywhere; }
.article-card h3 a { color: var(--text); text-decoration: none; }
.article-card h3 a:hover { color: var(--accent); text-decoration: underline; }
.article-card p { margin: 0; font-size: .85rem; color: var(--muted); overflow-wrap: anywhere; }
.more-link { margin: 12px 0 0; text-align: right; font-size: .9rem; }
.more-link a, .breadcrumb a { color: var(--accent); }
.site-header { padding: 14px 0; }
.site-header .container { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.site-name { font-weight: 800; font-size: 1.2rem; color: var(--text); text-decoration: none; }
.site-nav { display: flex; gap: 16px; font-size: .9rem; }
.site-nav a { color: var(--muted); text-decoration: none; }
.site-nav a:hover { color: var(--accent); }
.breadcrumb { margin: 20px 0 8px; font-size: .8rem; color: var(--muted); overflow-wrap: anywhere; }
.page-title { margin: 8px 0 4px; font-size: 1.6rem; }
.post {
  max-width: 760px; margin: 8px auto 32px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 14px; padding: clamp(18px, 4vw, 40px); box-shadow: var(--shadow);
}
.post-header h1 { margin: 0 0 10px; font-size: clamp(1.4rem, 4vw, 1.9rem); line-height: 1.4; overflow-wrap: anywhere; }
.post-meta { font-size: .82rem; color: var(--muted); }
.post-tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 0; }
.pr-note {
  margin: 18px 0 0; padding: 10px 14px; border-radius: 8px; font-size: .82rem;
  background: var(--bg); color: var(--muted); display: flex; gap: 8px; align-items: center;
}
.prose { margin-top: 24px; font-size: 1rem; line-height: 1.9; overflow-wrap: anywhere; }
.prose h2 { font-size: 1.3rem; margin: 2em 0 .6em; padding-bottom: .3em; border-bottom: 2px solid var(--border); }
.prose h3 { font-size: 1.1rem; margin: 1.6em 0 .5em; }
.prose p { margin: 0 0 1.1em; }
.prose a { color: var(--accent); }
.prose img { max-width: 100%; height: auto; border-radius: 8px; }
.prose img.af-pixel { width: 1px; height: 1px; }
.prose ul, .prose ol { padding-left: 1.4em; margin: 0 0 1.1em; }
.prose li { margin-bottom: .3em; }
.prose blockquote { margin: 0 0 1.1em; padding: .4em 1em; border-left: 4px solid var(--border); color: var(--muted); }
.prose code { background: var(--bg); padding: .1em .35em; border-radius: 4px; font-size: .9em; }
.prose pre { background: var(--bg); padding: 12px 14px; border-radius: 8px; overflow-x: auto; }
.prose pre code { background: none; padding: 0; }
.prose table { width: 100%; border-collapse: collapse; margin: 0 0 1.1em; font-size: .92rem; display: block; overflow-x: auto; }
.prose th, .prose td { border: 1px solid var(--border); padding: 8px 10px; text-align: left; }
.prose th { background: var(--bg); }
.related { max-width: 760px; margin: 0 auto 40px; }
.not-found { text-align: center; padding: 64px 0 80px; }
.not-found-links { text-align: center; }
footer {
  background: var(--surface); border-top: 1px solid var(--border);
  padding: 32px 0 24px; font-size: .82rem; color: var(--muted);
}
.footer-grid {
  display: grid; gap: 24px 32px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
}
.footer-grid section { min-width: 0; }
.footer-grid .policy { grid-column: 1 / -1; }
footer .footer-menu { list-style: none; padding: 0; margin: 0 0 10px; }
footer .footer-menu li { margin-bottom: 4px; }
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
.contact-btn {
  display: inline-block; margin-top: 4px; padding: 8px 18px; border-radius: 8px; font-weight: 600;
  background: var(--accent); color: #fff !important; text-decoration: none;
}
.contact-btn:hover { background: var(--accent-hover); }
"""

# 値札アイコンのファビコン（外部ファイル不要のインライン SVG）
FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='8' fill='%232563eb'/%3E"
    "%3Cpath d='M24.6 17.4l-7.2 7.2a1.9 1.9 0 0 1-2.7 0L7 17V7h10l7.6 7.6a1.9 1.9 0 0 1 0 2.8z' "
    "fill='none' stroke='%23fff' stroke-width='2.2' stroke-linejoin='round'/%3E"
    "%3Ccircle cx='11.5' cy='11.5' r='1.8' fill='%23fff'/%3E%3C/svg%3E"
)

NEWS_INITIAL_COUNT = 18  # 「すべて」表示で最初に見せるニュースの件数（残りは「もっと見る」）
SALE_SECTION_COUNT = 6  # 「最新のセール速報」欄の件数

# JavaScript が無効でも全件が表示される（検索エンジンにも全件が見える）よう、隠す処理は JS 側だけで行う
JS = """
(function () {
  var LIMIT = %d;
  var current = 'all';
  var expanded = false;
  var more = document.getElementById('more-news');
  function apply() {
    var shown = 0, rest = 0;
    document.querySelectorAll('.grid .card').forEach(function (card) {
      var match = current === 'all' || card.dataset.category === current;
      var visible = match && (expanded || current !== 'all' || shown < LIMIT);
      if (match && !visible) rest++;
      if (visible) shown++;
      card.hidden = !visible;
    });
    if (more) {
      more.hidden = rest === 0;
      more.querySelector('.more-count').textContent = rest;
    }
  }
  function select(cat) {
    current = cat;
    document.querySelectorAll('.filters button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.filter === cat));
    });
    apply();
  }
  document.querySelectorAll('.filters button').forEach(function (btn) {
    btn.addEventListener('click', function () { select(btn.dataset.filter); });
  });
  document.querySelectorAll('[data-show-filter]').forEach(function (link) {
    link.addEventListener('click', function () { select(link.dataset.showFilter); });
  });
  if (more) more.addEventListener('click', function () { expanded = true; apply(); });
  apply();
})();
""" % NEWS_INITIAL_COUNT


SALE_LABEL = "セール"
FILTER_ORDER = [SALE_LABEL, "製品ニュース", "IT・ネット"]


def display_category(item: Item) -> str:
    """タイトルにセール・値下げ関連の語があれば「セール」、なければ情報元のカテゴリ。"""
    return SALE_LABEL if is_sale_title(item.title) else item.category


def render_card(item: Item) -> str:
    e = html.escape
    date_html = ""
    if item.published:
        date_html = (
            f'<time datetime="{item.published.isoformat()}">'
            f'{item.published.strftime("%Y/%m/%d %H:%M")}</time>'
        )
    label = display_category(item)
    tag_class = "tag deal" if label == SALE_LABEL else "tag"
    return f"""
      <article class="card" data-category="{e(label)}">
        <div class="card-head">
          <span class="{tag_class}">{e(label)}</span>
          <span>{e(item.source)}</span>
          {date_html}
        </div>
        <h3>{render_title_link(item.title, item.link)}</h3>
        <p>{e(item.summary)}</p>
        {render_link(item.link, "続きを読む →")}
      </article>"""


def render_title_link(title: str, url: str) -> str:
    """記事タイトルを元記事へのリンクにする（楽天市場ならアフィリエイトリンク）。"""
    e = html.escape
    if not url:
        return e(title)
    if is_rakuten_ichiba_url(url):
        return (
            f'<a href="{e(build_moshimo_rakuten_link(url))}" target="_blank" rel="nofollow sponsored noopener" '
            f'referrerpolicy="no-referrer-when-downgrade" attributionsrc>{e(title)}</a>'
        )
    return f'<a href="{e(url)}" target="_blank" rel="noopener">{e(title)}</a>'


# ---------------------------------------------------------------------------
# SNS シェア（外部スクリプトを読み込まない、ただのリンク）
# ---------------------------------------------------------------------------

X_MAX_WEIGHT = 280  # X の投稿上限（日本語は1文字=2、URL は一律23として数える）
X_URL_WEIGHT = 23
X_HASHTAGS = "#セール #お買い得"
X_ACCOUNT = "mihariban_sale"  # サイト公式の X アカウント（@ なし）
X_PROFILE_URL = f"https://x.com/{X_ACCOUNT}"


def x_weight(text: str) -> int:
    """X の文字数の数え方（英数字・記号の一部は1、日本語などは2）で長さを返す。"""
    total = 0
    for ch in text:
        cp = ord(ch)
        light = cp <= 0x10FF or 0x2000 <= cp <= 0x200D or 0x2010 <= cp <= 0x201F or 0x2032 <= cp <= 0x2037
        total += 1 if light else 2
    return total


def x_intent_url(text: str, url: str, via: str = "") -> str:
    """X の投稿画面を開く URL。via を付けると投稿の末尾に「via @アカウント」が入る。"""
    href = f"https://twitter.com/intent/tweet?text={quote(text, safe='')}&url={quote(url, safe='')}"
    return href + (f"&via={quote(via, safe='')}" if via else "")


def build_sale_post_text(sales: list[Item], limit: int = 3) -> str:
    """「今日のセール速報」の X 投稿文を、上限に収まるように作る。"""
    budget = X_MAX_WEIGHT - X_URL_WEIGHT - 1  # URL と区切りの空白
    lines = ["【今日のセール速報】"]
    for item in sales[:limit]:
        # 先頭の【本日みつけたお買い得品】のような見出しは文字数がもったいないので省く
        title = re.sub(r"^【[^】]{1,20}】\s*", "", item.title) or item.title
        title = title if len(title) <= 40 else title[:39] + "…"
        candidate = "\n".join(lines + ["・" + title, X_HASHTAGS])
        if x_weight(candidate) > budget:
            break
        lines.append("・" + title)
    return "\n".join(lines + [X_HASHTAGS])


def render_share_links(title: str, url: str, x_text: str | None = None) -> str:
    """X・LINE・はてなブックマークのシェアボタン。"""
    e = html.escape
    x_href = x_intent_url(x_text if x_text is not None else title, url, via=X_ACCOUNT)
    line_href = f"https://social-plugins.line.me/lineit/share?url={quote(url, safe='')}"
    hatena_href = f"https://b.hatena.ne.jp/add?mode=confirm&url={quote(url, safe='')}&title={quote(title, safe='')}"
    return (
        '<div class="share" role="group" aria-label="シェア">'
        f'<a class="share-btn share-x" href="{e(x_href)}" target="_blank" rel="noopener">X でポスト</a>'
        f'<a class="share-btn share-line" href="{e(line_href)}" target="_blank" rel="noopener">LINE で送る</a>'
        f'<a class="share-btn share-hatena" href="{e(hatena_href)}" target="_blank" rel="noopener">はてブ</a>'
        "</div>"
    )


def render_sale_section(items: list[Item], exclude_links: set[str]) -> str:
    """タイトルにセール関連の語を含む最新ニュースを、トップページ上部にまとめて表示する。"""
    e = html.escape
    all_sales = [i for i in items if is_sale_title(i.title)]
    sales = [i for i in all_sales if i.link and i.link not in exclude_links][:SALE_SECTION_COUNT]
    if not sales:
        return ""
    today = datetime.now(JST).date()
    rows = []
    for i in sales:
        when = ""
        if i.published:
            when = ("今日 " if i.published.date() == today else i.published.strftime("%m/%d ")) + i.published.strftime("%H:%M")
        summary = i.summary if len(i.summary) <= 70 else i.summary[:70].rstrip() + "…"
        rows.append(f"""
        <article class="sale-item">
          <div class="sale-meta"><span class="tag deal">{SALE_LABEL}</span><span>{e(i.source)}</span><time>{e(when)}</time></div>
          <h3>{render_title_link(i.title, i.link)}</h3>
          <p>{e(summary)}</p>
        </article>""")
    return f"""
    <section class="sales" id="sale" aria-labelledby="sale-title">
      <h2 class="section-title" id="sale-title">最新のセール速報</h2>
      <p class="section-note">各メディアの最新ニュースのうち、セール・値下げ・割引に関するものを自動で表示しています。価格は掲載時点のものです。</p>
      <div class="sale-items">{"".join(rows)}
      </div>
      <div class="sale-footer">
        <a class="share-btn share-x" href="{e(x_intent_url(build_sale_post_text(sales), SITE_URL))}" target="_blank" rel="noopener">今日のセール速報を X でポスト</a>
        <a class="more-link-inline" href="#news" data-show-filter="{SALE_LABEL}">セールのニュースをすべて見る（{len(all_sales)} 件）→</a>
      </div>
    </section>"""


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


def render_pick(p: Pick) -> str:
    e = html.escape
    is_rakuten = is_rakuten_ichiba_url(p.url)
    image = (
        f'<div class="pick-img"><img src="{e(p.image)}" alt="" loading="lazy" width="96" height="96"></div>'
        if p.image else ""
    )
    meta = " ・ ".join(x for x in (p.source, p.date) if x)
    label = "楽天市場で見る" if is_rakuten else ("詳しく見る" if p.manual else "続きを読む →")
    return f"""
        <article class="pick{' manual' if p.manual else ''}">
          {image}
          <div class="pick-body">
            <span class="pick-label">{e(p.label)}</span>
            <h3>{e(p.title)}</h3>
            {f'<p>{e(p.comment)}</p>' if p.comment else ''}
            {f'<div class="pick-price">{e(p.price)}</div>' if p.price else ''}
            {f'<div class="pick-meta">{e(meta)}</div>' if meta else ''}
            {render_link(p.url, label)}
          </div>
        </article>"""


def render_picks(picks: list[Pick]) -> str:
    if not picks:
        return ""
    has_ad = any(is_rakuten_ichiba_url(p.url) for p in picks)
    pr = ' <span class="pr">PR</span>' if has_ad else ""
    note = (
        "編集部が注目している商品・セールをピックアップしています。価格・在庫は変動するため、購入前にリンク先でご確認ください。"
        if picks[0].manual
        else "セール・値下げに関する最新ニュースから自動でピックアップしています。"
    )
    return f"""
    <section class="pickup" aria-labelledby="pickup-title">
      <h2 class="section-title" id="pickup-title">今週の注目・編集部ピックアップ{pr}</h2>
      <p class="section-note">{html.escape(note)}</p>
      <div class="pickups">{"".join(render_pick(p) for p in picks)}
      </div>
    </section>"""


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
    </section>"""


def _json_ld(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def render_footer() -> str:
    e = html.escape
    sources = "、".join(e(f["name"]) for f in FEEDS)
    contact_parts = []
    if CONTACT_FORM_URL:
        contact_parts.append(
            f'<p><a class="contact-btn" href="{e(CONTACT_FORM_URL)}" target="_blank" rel="noopener">お問い合わせフォーム</a></p>'
        )
    if CONTACT_EMAIL:
        contact_parts.append(f'<p>メール: <a href="mailto:{e(CONTACT_EMAIL)}">{e(CONTACT_EMAIL)}</a></p>')
    if contact_parts:
        contact_html = "<p>お気づきの点や掲載停止のご連絡は、こちらからお問い合わせください。</p>" + "".join(contact_parts)
    else:
        contact_html = "<p>お問い合わせ窓口は準備中です。</p>"
    year = datetime.now(JST).year
    return f"""
  <footer>
    <div class="container">
      <div class="footer-grid">
        <section>
          <h3>運営者情報</h3>
          <p>{e(OPERATOR_NAME)}</p>
          <p><a href="/articles/how-to-use/">このサイトについて</a></p>
        </section>
        <section>
          <h3>メニュー</h3>
          <ul class="footer-menu">
            <li><a href="/">トップページ</a></li>
            <li><a href="/articles/">記事一覧</a></li>
            <li><a href="/feed.xml">RSS（記事の更新情報）</a></li>
          </ul>
          <p><a class="follow-x" href="{X_PROFILE_URL}" target="_blank" rel="noopener">X でフォロー（@{X_ACCOUNT}）</a></p>
        </section>
        <section>
          <h3>お問い合わせ</h3>
          {contact_html}
        </section>
        <section class="policy">
          <h3>プライバシーポリシー</h3>
          <ul>
            <li>当サイトでは、アクセス解析ツール（Cloudflare Web Analytics）および広告配信（Amazonアソシエイト、楽天アフィリエイト等のアフィリエイトプログラム）を利用しています。</li>
            <li>Cloudflare Web Analytics はCookieを使用せず、個人を特定しない形でページの閲覧状況を集計します。</li>
            <li>アフィリエイトプログラムはCookieを使用してトラフィックデータやアクセス情報を収集する場合がありますが、個人を特定する情報は含みません。ブラウザの設定でCookieを無効にすることも可能です。</li>
          </ul>
        </section>
      </div>
      <div class="footer-bottom">
        <p class="disclosure">当サイトはアフィリエイト広告（Amazonアソシエイト含む）を掲載しています。Amazonのアソシエイトとして、当メディアは適格販売により収入を得ています。</p>
        <p>情報元: {sources}。各記事の著作権は配信元に帰属します。</p>
        <p>&copy; {year} {e(OPERATOR_NAME)}</p>
      </div>
    </div>
  </footer>"""


def render_document(
    *,
    title: str,
    description: str,
    path: str,
    header_html: str,
    main_html: str,
    json_ld: dict,
    og_type: str = "website",
    og_image: str = "",
    script: str = "",
) -> str:
    """全ページ共通の HTML（head・フッター）で本文を包む。path はサイト内パス（トップは空文字）。"""
    e = html.escape
    url = SITE_URL + path
    image = og_image or SITE_URL + OGP_IMAGE
    if image.startswith("/"):
        image = SITE_URL + image.lstrip("/")
    script_html = f"\n  <script>{script}</script>" if script else ""
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)}</title>
  <meta name="description" content="{e(description)}">
  <link rel="canonical" href="{e(url)}">
  <link rel="alternate" type="application/rss+xml" title="{e(SITE_TITLE)} の記事" href="{e(SITE_URL)}feed.xml">
  <meta name="google-site-verification" content="{e(GOOGLE_SITE_VERIFICATION)}">
  <meta name="msvalidate.01" content="{e(BING_SITE_VERIFICATION)}">
  <meta name="theme-color" content="#2563eb">
  <link rel="icon" href="{FAVICON}">
  <meta property="og:type" content="{e(og_type)}">
  <meta property="og:site_name" content="{e(SITE_TITLE)}">
  <meta property="og:title" content="{e(title)}">
  <meta property="og:description" content="{e(description)}">
  <meta property="og:url" content="{e(url)}">
  <meta property="og:image" content="{e(image)}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:locale" content="ja_JP">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:site" content="@{X_ACCOUNT}">
  <script type="application/ld+json">{_json_ld(json_ld)}</script>
  <style>{CSS}</style>
</head>
<body>{header_html}
  <main class="container">{main_html}
  </main>{render_footer()}{script_html}
</body>
</html>
"""


def render_site_header() -> str:
    """記事ページ用のコンパクトなヘッダー（サイト名はトップへのリンク）。"""
    e = html.escape
    return f"""
  <header class="site-header">
    <div class="container">
      <a class="site-name" href="/">{e(SITE_TITLE)}</a>
      <nav class="site-nav" aria-label="サイト内"><a href="/">トップ</a><a href="/articles/">記事一覧</a></nav>
    </div>
  </header>"""


def render_article_card(a: Article) -> str:
    e = html.escape
    tags = "".join(f'<span class="tag">{e(t)}</span>' for t in a.tags[:3])
    return f"""
        <article class="article-card">
          <div class="card-head">{tags}<time datetime="{a.date.isoformat()}">{a.date.strftime("%Y/%m/%d")}</time></div>
          <h3><a href="/{e(a.path)}">{e(a.title)}</a></h3>
          <p>{e(a.description)}</p>
        </article>"""


def render_article_section(articles: list[Article]) -> str:
    if not articles:
        return ""
    cards = "".join(render_article_card(a) for a in articles[:TOP_ARTICLE_COUNT])
    more = '<p class="more-link"><a href="/articles/">記事一覧へ →</a></p>' if len(articles) > TOP_ARTICLE_COUNT else ""
    return f"""
    <section class="articles" aria-labelledby="articles-title">
      <h2 class="section-title" id="articles-title">編集部の記事</h2>
      <div class="article-cards">{cards}
      </div>{more}
    </section>"""


def render_page(
    items: list[Item],
    rankings: list[tuple[str, list[Product]]] | None = None,
    picks: list[Pick] | None = None,
    articles: list[Article] | None = None,
) -> str:
    e = html.escape
    now = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")
    counts: dict[str, int] = {}
    for i in items:
        label = display_category(i)
        counts[label] = counts.get(label, 0) + 1
    categories = [c for c in FILTER_ORDER if c in counts] + sorted(c for c in counts if c not in FILTER_ORDER)
    filter_buttons = (
        f'<button type="button" data-filter="all" aria-pressed="true">すべて <span class="count">{len(items)}</span></button>'
        + "".join(
            f'<button type="button" data-filter="{e(c)}" aria-pressed="false">{e(c)} <span class="count">{counts[c]}</span></button>'
            for c in categories
        )
    )
    cards = "".join(render_card(i) for i in items) or '<p class="empty">現在表示できる記事がありません。</p>'
    articles = articles or []
    # 自動ピックアップに出した記事は、セール速報欄に重ねて出さない
    picked_links = {p.url for p in (picks or []) if not p.manual}
    sale_section = render_sale_section(items, picked_links)
    news_heading = '\n    <h2 class="section-title" id="news">最新ニュース</h2>'
    about_html = e(SITE_ABOUT).replace(e(SITE_TITLE), "<strong>" + e(SITE_TITLE) + "</strong>", 1)
    nav_links = ([('#sale', 'セール速報')] if sale_section else []) + [("#news", "ニュース"), ("/articles/", "記事一覧")]
    top_nav = "".join(f'<a href="{href}">{label}</a>' for href, label in nav_links)
    header_html = f"""
  <header>
    <div class="container">
      <nav class="top-nav" aria-label="サイト内">{top_nav}</nav>
      <h1>{e(SITE_TITLE)}</h1>
      <p>{e(SITE_DESCRIPTION)}</p>
      <div class="about" role="note" aria-label="サイト概要">{about_html}</div>
      <div class="meta">最終更新: {now} (JST) ・ {len(items)} 件</div>
    </div>
  </header>"""
    main_html = f"""{render_picks(picks or [])}{sale_section}{render_article_section(articles)}{render_rankings(rankings or [])}{news_heading}
    <nav class="filters" aria-label="カテゴリ絞り込み">{filter_buttons}</nav>
    <section class="grid" aria-labelledby="news">{cards}
    </section>
    <div class="more-wrap"><button type="button" id="more-news" class="more-btn" hidden>もっと見る（残り <span class="more-count"></span> 件）</button></div>"""
    return render_document(
        title=f"{SITE_TITLE} | {SITE_DESCRIPTION}",
        description=SITE_ABOUT,
        path="",
        header_html=header_html,
        main_html=main_html,
        json_ld={
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": SITE_TITLE,
            "url": SITE_URL,
            "description": SITE_ABOUT,
            "inLanguage": "ja",
            "publisher": {"@type": "Organization", "name": OPERATOR_NAME},
        },
        script=JS,
    )


def render_article_page(a: Article, articles: list[Article]) -> str:
    e = html.escape
    tags = "".join(f'<span class="tag">{e(t)}</span>' for t in a.tags)
    updated = (
        f' ・ 更新 <time datetime="{a.updated.isoformat()}">{a.updated.strftime("%Y/%m/%d")}</time>'
        if a.updated and a.updated != a.date else ""
    )
    pr_note = (
        '<p class="pr-note"><span class="pr">PR</span> この記事にはアフィリエイト広告（楽天市場へのリンク）が含まれています。</p>'
        if a.has_affiliate else ""
    )
    others = [x for x in articles if x.slug != a.slug][:3]
    related = (
        f"""
      <section class="related" aria-labelledby="related-title">
        <h2 class="section-title" id="related-title">ほかの記事</h2>
        <div class="article-cards">{"".join(render_article_card(x) for x in others)}
        </div>
      </section>"""
        if others else ""
    )
    main_html = f"""
    <nav class="breadcrumb" aria-label="パンくずリスト"><a href="/">トップ</a> › <a href="/articles/">記事一覧</a> › <span>{e(a.title)}</span></nav>
    <article class="post">
      <header class="post-header">
        <h1>{e(a.title)}</h1>
        <div class="post-meta"><time datetime="{a.date.isoformat()}">{a.date.strftime("%Y/%m/%d")}</time>{updated} ・ {e(OPERATOR_NAME)}</div>
        <div class="post-tags">{tags}</div>
      </header>
      {pr_note}
      <div class="prose">{a.body_html}</div>
      <div class="post-share"><p>この記事が役に立ったらシェアしてください</p>{render_share_links(a.title, SITE_URL + a.path, f"{a.title} | {SITE_TITLE}")}
        <p class="follow-note">毎朝のセール速報は <a href="{X_PROFILE_URL}" target="_blank" rel="noopener">X（@{X_ACCOUNT}）</a> でもお届けしています。</p></div>
    </article>{related}"""
    return render_document(
        title=f"{a.title} | {SITE_TITLE}",
        description=a.description,
        path=a.path,
        header_html=render_site_header(),
        main_html=main_html,
        og_type="article",
        og_image=a.image,
        json_ld={
            "@context": "https://schema.org",
            "@type": "BlogPosting",
            "headline": a.title,
            "description": a.description,
            "datePublished": a.date.isoformat(),
            "dateModified": a.last_modified.isoformat(),
            "inLanguage": "ja",
            "mainEntityOfPage": SITE_URL + a.path,
            "author": {"@type": "Organization", "name": OPERATOR_NAME},
            "publisher": {"@type": "Organization", "name": OPERATOR_NAME},
        },
    )


def render_article_index(articles: list[Article]) -> str:
    cards = "".join(render_article_card(a) for a in articles) or '<p class="empty">記事はまだありません。</p>'
    main_html = f"""
    <nav class="breadcrumb" aria-label="パンくずリスト"><a href="/">トップ</a> › <span>記事一覧</span></nav>
    <h1 class="page-title">記事一覧</h1>
    <p class="section-note">{html.escape(OPERATOR_NAME)} が書いた、スマホ・パソコン・家電の選び方やセール情報の記事です。</p>
    <div class="article-cards">{cards}
    </div>"""
    return render_document(
        title=f"記事一覧 | {SITE_TITLE}",
        description=f"{SITE_TITLE} 編集部の記事一覧。スマホ・パソコン・家電の選び方やセール情報をまとめています。",
        path="articles/",
        header_html=render_site_header(),
        main_html=main_html,
        json_ld={
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": f"記事一覧 | {SITE_TITLE}",
            "url": SITE_URL + "articles/",
            "inLanguage": "ja",
        },
    )


def render_not_found_page() -> str:
    """存在しない URL 用のページ。Cloudflare Pages は dist/404.html があると 404 で返す。"""
    main_html = """
    <section class="not-found">
      <h1 class="page-title">ページが見つかりません</h1>
      <p class="section-note">お探しのページは移動または削除された可能性があります。</p>
      <p class="more-link not-found-links"><a href="/">トップページへ</a> ・ <a href="/articles/">記事一覧へ</a></p>
    </section>"""
    page = render_document(
        title=f"ページが見つかりません | {SITE_TITLE}",
        description="お探しのページは見つかりませんでした。",
        path="404.html",
        header_html=render_site_header(),
        main_html=main_html,
        json_ld={"@context": "https://schema.org", "@type": "WebPage", "name": "404"},
    )
    # 検索結果に載せない
    return page.replace("<head>\n", '<head>\n  <meta name="robots" content="noindex">\n', 1)


def write_article_pages(articles: list[Article]) -> None:
    """dist/articles/ を作り直し、記事一覧と各記事のページを出力する（削除した記事はページも消える）。"""
    out = OUTPUT_PATH.parent / "articles"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "index.html").write_text(render_article_index(articles), encoding="utf-8")
    for a in articles:
        page_dir = out / a.slug
        page_dir.mkdir()
        (page_dir / "index.html").write_text(render_article_page(a, articles), encoding="utf-8")


FEED_MAX_ITEMS = 20


def render_rss_feed(articles: list[Article]) -> str:
    """編集部の記事の RSS 2.0 フィード（ニュースは他サイトの記事なので含めない）。"""
    e = html.escape

    def rfc822(d) -> str:
        return format_datetime(datetime(d.year, d.month, d.day, 7, 0, tzinfo=JST))

    items = "".join(
        f"""
    <item>
      <title>{e(a.title)}</title>
      <link>{e(SITE_URL + a.path)}</link>
      <guid isPermaLink="true">{e(SITE_URL + a.path)}</guid>
      <pubDate>{rfc822(a.date)}</pubDate>
      <description>{e(a.description)}</description>
    </item>"""
        for a in articles[:FEED_MAX_ITEMS]
    )
    last = rfc822(articles[0].last_modified) if articles else rfc822(datetime.now(JST).date())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{e(SITE_TITLE)}</title>
    <link>{e(SITE_URL)}</link>
    <description>{e(SITE_DESCRIPTION)}</description>
    <language>ja</language>
    <lastBuildDate>{last}</lastBuildDate>
    <atom:link href="{e(SITE_URL)}feed.xml" rel="self" type="application/rss+xml"/>{items}
  </channel>
</rss>
"""


def write_seo_files(articles: list[Article] | None = None) -> None:
    """検索エンジン向けの robots.txt・sitemap.xml・IndexNow キーファイルを dist/ に出力する。"""
    out = OUTPUT_PATH.parent
    (out / f"{INDEXNOW_KEY}.txt").write_text(INDEXNOW_KEY, encoding="utf-8")
    # Bing Webmaster Tools の XML ファイル認証（meta タグ認証の代替）
    (out / "BingSiteAuth.xml").write_text(
        '<?xml version="1.0"?>\n<users>\n\t<user>'
        f"{html.escape(BING_SITE_VERIFICATION)}</user>\n</users>\n",
        encoding="utf-8",
    )
    (out / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}sitemap.xml\n", encoding="utf-8"
    )
    today = datetime.now(JST).date()
    articles = articles or []
    entries = [(SITE_URL, today, "daily"), (SITE_URL + "articles/", articles[0].last_modified if articles else today, "weekly")]
    entries += [(SITE_URL + a.path, a.last_modified, "monthly") for a in articles]
    urls = "".join(
        f"  <url>\n    <loc>{html.escape(loc)}</loc>\n    <lastmod>{mod.isoformat()}</lastmod>\n"
        f"    <changefreq>{freq}</changefreq>\n  </url>\n"
        for loc, mod, freq in entries
    )
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}</urlset>\n",
        encoding="utf-8",
    )


def main() -> int:
    items = collect_items()
    if not items:
        print("[ERROR] 1件も取得できませんでした。既存の index.html は更新しません。", file=sys.stderr)
        return 1
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    rankings = collect_rakuten_rankings()
    picks = select_picks(items)
    today = datetime.now(JST).date()
    deals_log = update_log(items, DEALS_LOG_PATH, today)
    maybe_write_weekly_digest(
        deals_log, picks, ARTICLES_DIR, today, force=os.environ.get("FORCE_WEEKLY_DIGEST") == "1"
    )
    articles = load_articles(ARTICLES_DIR, urlparse(SITE_URL).hostname or "")
    print(f"[INFO] 独自記事: {len(articles)} 件")
    OUTPUT_PATH.write_text(render_page(items, rankings, picks, articles), encoding="utf-8")
    write_article_pages(articles)
    (OUTPUT_PATH.parent / "404.html").write_text(render_not_found_page(), encoding="utf-8")
    write_seo_files(articles)
    (OUTPUT_PATH.parent / "feed.xml").write_text(render_rss_feed(articles), encoding="utf-8")
    print(f"[INFO] {OUTPUT_PATH} を生成しました（{len(items)} 件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
