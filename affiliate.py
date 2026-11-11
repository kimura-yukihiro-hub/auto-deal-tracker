"""アフィリエイトリンクの変換処理（トップページ・記事ページ共通）。

リンク先が楽天市場（*.rakuten.co.jp）の場合のみ、もしもアフィリエイトの
「どこでもリンク」形式に変換し、インプレッション計測用の 1x1 ピクセルを付与する。
"""

from __future__ import annotations

from urllib.parse import quote, urlencode, urlparse

# もしもアフィリエイト × 楽天市場「どこでもリンク」の本番パラメータ
MOSHIMO_RAKUTEN_PARAMS = {"a_id": "5891942", "p_id": "54", "pc_id": "54", "pl_id": "616"}
MOSHIMO_CLICK_URL = "https://af.moshimo.com/af/c/click"
MOSHIMO_IMPRESSION_URL = "https://i.moshimo.com/af/i/impression"

# アフィリエイトリンクに付与する属性（もしも指定 + Google 推奨の sponsored）
AFFILIATE_LINK_ATTRS = {
    "target": "_blank",
    "rel": "nofollow sponsored noopener",
    "referrerpolicy": "no-referrer-when-downgrade",
    "attributionsrc": "",
}


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
