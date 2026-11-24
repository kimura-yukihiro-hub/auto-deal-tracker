"""記事に書いた楽天の商品の価格が、今の価格と大きく違っていないかを確かめ、違っていれば Issue で知らせる（月 1 回）。

  python price_check.py            # 確かめて、直す所があれば Issue を作る（GITHUB_TOKEN が要る）
  python price_check.py --dry-run  # 確かめて、結果を表示するだけ

記事の同じ行に「楽天の商品リンク」と「約○円」が 1 つずつあるものを組にする（早見表の行・「おすすめ：」の行）。
記事は書き換えない（価格を直すかどうかは、人が商品ページを見て決める）。
あわせて、記事のほかのリンク（メーカーの公式ページ・組にならなかった楽天の商品）と商品の画像が、
なくなっていないか（404・410・つながらない）も見る。ニュースのリンクは古くなるのが当たり前なので見ない。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ARTICLES = ROOT / "articles"
THRESHOLD = 0.10            # 10% 以上ずれていたら知らせる
ISSUE_TITLE = "🔎 記事の商品の価格の見直し"
UA = {"User-Agent": "Mozilla/5.0 (compatible; sale-watch-price-check/1.0)"}
LINK_RE = re.compile(r"\]\((https://item\.rakuten\.co\.jp/[^)\s]+)\)")
PRICE_RE = re.compile(r"約?\s*(\d{1,3}(?:,\d{3})+|\d{3,7})\s*円")
URL_RE = re.compile(r'\]\((https?://[^)\s]+)\)|src="(https?://[^"]+)"')
# 見ないもの：ニュースの記事（古いものは消えることがある）、自分のサイト、問い合わせのフォーム、書き方の見本
SKIP_HOSTS = ("pc.watch.impress.co.jp", "itmedia.co.jp", "getnavi.jp", "gizmodo.jp", "gamewith.jp",
              "auto-deal-tracker.pages.dev", "forms.gle", "example.com")
PAGE_PRICE_RE = re.compile(r'"(?:taxIncludedPrice|itemPrice|price)"\s*:\s*\{?[^{}]{0,80}?(\d{3,7})')


def pairs_in(text: str) -> list[tuple[str, int]]:
    """記事の本文から (商品ページの URL, 書いた価格) の組を取り出す。リンクと価格が 1 つずつある行だけ。"""
    out = []
    for line in text.splitlines():
        links = LINK_RE.findall(line)
        prices = PRICE_RE.findall(LINK_RE.sub("", line))
        if len(links) == 1 and len(prices) == 1:
            out.append((links[0], int(prices[0].replace(",", ""))))
    return out


def published_articles() -> list[Path]:
    return [p for p in sorted(ARTICLES.glob("*.md"))
            if not p.name.startswith("_") and not re.search(r"(?m)^draft:\s*(true|yes|1)\s*$", p.read_text(encoding="utf-8"))]


def page_price(url: str) -> tuple[str, int | None]:
    """(状態, 今の価格)。状態は ok / gone（ページがない）/ unknown（読めなかった）。"""
    try:
        r = requests.get(url, headers=UA, timeout=30)
    except requests.RequestException:
        return "unknown", None
    if r.status_code == 404:
        return "gone", None
    if r.status_code != 200:
        return "unknown", None
    m = re.search(rb'charset=["\']?([A-Za-z0-9_-]+)', r.content[:3000])
    text = r.content.decode((m.group(1).decode() if m else "utf-8"), "replace")
    prices = sorted({int(x) for x in PAGE_PRICE_RE.findall(text) if 100 <= int(x) <= 5_000_000})
    return ("ok", prices[0]) if prices else ("unknown", None)


def link_state(url: str) -> str:
    """ok / gone（ページがない・つながらない）/ unknown（断られた・一時的なエラー）。"""
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"},
                         timeout=30, allow_redirects=True)
    except requests.ConnectionError:
        return "gone"
    except requests.RequestException:
        return "unknown"
    return "gone" if r.status_code in (404, 410) else ("ok" if r.status_code < 400 else "unknown")


def other_links(path: Path, checked: set[str]) -> list[str]:
    """価格の組で見なかったリンクと画像のうち、なくなったものを知らせる行。"""
    from urllib.parse import urlparse
    lines = []
    for m in URL_RE.finditer(path.read_text(encoding="utf-8")):
        url = m.group(1) or m.group(2)
        host = urlparse(url).hostname or ""
        if url in checked or host.endswith(SKIP_HOSTS):
            continue
        checked.add(url)
        time.sleep(1)
        if link_state(url) == "gone":
            kind = "画像" if m.group(2) else "リンク"
            lines.append(f"- `articles/{path.name}`：{kind}の先がなくなっています（404 かつながらない）{url}")
    return lines


def check() -> list[str]:
    lines, seen, cache = [], set(), {}
    for path in published_articles():
        for url, written in pairs_in(path.read_text(encoding="utf-8")):
            if (path.name, url) in seen:   # 早見表とおすすめの両方に出てくる商品は 1 回だけ
                continue
            seen.add((path.name, url))
            if url not in cache:
                cache[url] = page_price(url)
                time.sleep(2)   # 楽天に負担をかけない
            state, now = cache[url]
            if state == "gone":
                lines.append(f"- `articles/{path.name}`：商品ページがなくなっています（販売終了の可能性）{url}")
            elif state == "ok" and abs(now - written) / written >= THRESHOLD:
                lines.append(f"- `articles/{path.name}`：記事は約{written:,}円 → 今は{now:,}円（{(now - written) / written:+.0%}）{url}")
            elif state == "unknown":
                print(f"[INFO] 価格を読めませんでした: {url}")
    checked = set(cache)
    for path in published_articles():
        lines += other_links(path, checked)
    return lines


def open_issue(lines: list[str]) -> None:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        print("[INFO] GITHUB_TOKEN がないので Issue は作りません")
        return
    body = ("記事に書いた楽天の商品の価格が、今の価格と 10% 以上違うもの・商品ページがなくなったもの、"
            "ほかのリンク（メーカーの公式ページなど）や商品の画像がなくなったものです。\n"
            "商品ページを見て、記事の価格と「○月○日時点」を直すか、商品を差し替えてください（Claude に頼んでもよい）。\n\n"
            + "\n".join(lines))
    r = requests.post(f"https://api.github.com/repos/{repo}/issues", timeout=30,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      data=json.dumps({"title": ISSUE_TITLE, "body": body}))
    print(f"[INFO] Issue を作りました: {r.json().get('html_url')}" if r.ok else f"[WARN] Issue を作れませんでした: HTTP {r.status_code}")


def main() -> int:
    lines = check()
    print("\n".join(lines) or "価格の大きな変わり・なくなったリンクはありませんでした")
    if lines and "--dry-run" not in sys.argv:
        open_issue(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
