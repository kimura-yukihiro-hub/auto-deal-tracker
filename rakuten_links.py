"""楽天市場のリンクの整理と、商品情報の取得。

- 楽天市場アプリの「共有」で取れる短いリンク（a.r10.to/…）を、元の商品ページの URL に戻す。
  戻さないと、affiliate.py のアフィリエイト変換（*.rakuten.co.jp だけが対象）が効かず、報酬が入らない。
  一度戻した結果は data/short_links.json に保存し、次からはネットに問い合わせない。
- 商品ページから、商品名（短く整えたもの）と価格を取る（Issue のコメントで下書きを仕上げるときに使う）。
"""

from __future__ import annotations

import json
import os
import re
import time
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "data" / "short_links.json"
SHORT_HOSTS = {"a.r10.to", "r10.to"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
TIMEOUT = 20
MAX_NAME_LENGTH = 32

_cache: dict[str, str] | None = None


def is_short_link(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in SHORT_HOSTS


def normalize(url: str) -> str | None:
    """楽天市場の URL を「https://item.rakuten.co.jp/ショップ/商品/」のような形にそろえる。楽天市場でなければ None。"""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host == "afl.rakuten.co.jp" or host.endswith(".afl.rakuten.co.jp"):
        # 他人のアフィリエイトリンクの場合は、中に入っている元の URL を取り出す
        inner = parse_qs(parsed.query).get("pc", [""])[0]
        return normalize(unquote(inner)) if inner else None
    if not (host == "rakuten.co.jp" or host.endswith(".rakuten.co.jp")):
        return None
    path = parsed.path or "/"
    return f"https://{host}{path}"  # 追跡用のパラメータ（?scid= など）は外す


def _load_cache() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is not None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def resolve(url: str) -> str | None:
    """短いリンクを楽天市場の商品ページの URL に戻す。戻せなければ None。"""
    if not is_short_link(url):
        return normalize(url)
    cache = _load_cache()
    if url in cache:
        return cache[url] or None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        final = normalize(resp.url)
    except requests.RequestException as e:
        print(f"[WARN] 短いリンクを戻せませんでした（次回また試します）: {url} ({type(e).__name__})")
        return None
    cache[url] = final or ""
    _save_cache()
    if not final:
        print(f"[WARN] 短いリンクの行き先が楽天市場ではありません: {url} → {resp.url}")
    return final


def expand_for_affiliate(url: str) -> str:
    """表示用: 短いリンクなら元の URL に戻す。戻せないときはそのまま返す（リンク切れにはしない）。"""
    if is_short_link(url):
        return resolve(url) or url
    return url


# ---------------------------------------------------------------------------
# 商品情報
# ---------------------------------------------------------------------------

def _decode(content: bytes) -> str:
    m = re.search(rb'charset=["\']?([\w-]+)', content[:3000])
    for enc in ([m.group(1).decode("ascii", "ignore")] if m else []) + ["utf-8", "euc_jp", "cp932"]:
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")


def short_name(title: str) -> str:
    """楽天の長い商品名を短くする: 【楽天1位】などの飾りとショップ名を外し、32 字程度に切る。"""
    name = unescape(title)
    name = re.sub(r"[：:][^：:]*(楽天市場店|市場店|ショップ|SHOP|shop|store|Store)[^：:]*$", "", name)
    name = re.sub(r"【[^】]*】|\[[^\]]*\]|［[^］]*］|★[^★]*★", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" 　/／|｜")
    if len(name) <= MAX_NAME_LENGTH:
        return name
    cut = name[:MAX_NAME_LENGTH]
    space = max(cut.rfind(" "), cut.rfind("　"))
    return (cut[:space] if space >= 12 else cut).strip() + "…"


# ---------------------------------------------------------------------------
# 商品検索（下書きの欄ごとの候補を出す）
# ---------------------------------------------------------------------------

# ランキング（fetch_and_build.py）と同じ新しい API を先に試し、なければ以前からある呼び出し先を使う
SEARCH_ENDPOINTS = [
    "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601",
    "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601",
]
MIN_REVIEW_AVERAGE = 4.0
MIN_REVIEW_COUNT = 10
CANDIDATES_PER_SLOT = 3


class SearchError(RuntimeError):
    pass


def _api_auth() -> tuple[dict, dict]:
    app_id = os.environ.get("RAKUTEN_APP_ID", "")
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY", "")
    site = os.environ.get("RAKUTEN_SITE_URL", "")
    if not app_id:
        raise SearchError("楽天のアプリID（RAKUTEN_APP_ID）が設定されていません")
    params = {"applicationId": app_id, "format": "json", "formatVersion": 2}
    if access_key:
        params["accessKey"] = access_key
    headers = dict(HEADERS)
    if site:
        parsed = urlparse(site)
        headers["Referer"] = site
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    return params, headers


def search_items(keyword: str, min_price: int | None = None, max_price: int | None = None) -> list[dict]:
    """在庫があり、レビューが多く評価の高い商品を最大 3 件返す。"""
    params, headers = _api_auth()
    params.update({
        "keyword": keyword[:120],
        "availability": 1,
        "sort": "-reviewCount",
        "hits": 30,
        "elements": "itemName,itemPrice,itemUrl,itemCode,shopName,reviewAverage,reviewCount",
    })
    if min_price:
        params["minPrice"] = int(min_price)
    if max_price:
        params["maxPrice"] = int(max_price)
    last_error = ""
    for endpoint in SEARCH_ENDPOINTS:
        try:
            resp = requests.get(endpoint, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            last_error = f"通信失敗 ({type(e).__name__})"
            continue
        if resp.status_code == 429:
            time.sleep(2)
            resp = requests.get(endpoint, params=params, headers=headers, timeout=TIMEOUT)
        if resp.status_code in (404, 405):
            last_error = f"{endpoint} が見つかりません (HTTP {resp.status_code})"
            continue
        if resp.status_code != 200:
            raise SearchError(f"HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        items = []
        for raw in data.get("Items", data.get("items", [])):
            it = raw.get("Item", raw) if isinstance(raw, dict) else {}
            url = normalize(it.get("itemUrl", "") or "")
            avg, count = float(it.get("reviewAverage") or 0), int(it.get("reviewCount") or 0)
            if not url or avg < MIN_REVIEW_AVERAGE or count < MIN_REVIEW_COUNT:
                continue
            items.append({"url": url, "name": short_name(re.sub(r"<[^>]+>", "", it.get("itemName", ""))),
                          "price": int(it.get("itemPrice") or 0) or None, "review_average": avg,
                          "review_count": count, "shop": it.get("shopName", "")})
            if len(items) >= CANDIDATES_PER_SLOT:
                break
        return items
    raise SearchError(last_error or "商品検索に失敗しました")


def fetch_item(url: str) -> dict:
    """商品ページから {"url", "name", "price"} を取る。取れなかった項目は空。"""
    item = {"url": url, "name": "", "price": None}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[WARN] 商品ページを開けませんでした: {url} ({type(e).__name__})")
        return item
    text = _decode(resp.content)
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', text)
    if m:
        item["name"] = short_name(m.group(1))
    m = re.search(r'itemprop="price"[^>]*content="([\d.]+)"', text) or re.search(r'"taxIncludedPrice"\s*:\s*(\d+)', text)
    if m:
        item["price"] = int(float(m.group(1)))
    return item
