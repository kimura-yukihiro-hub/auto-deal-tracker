#!/usr/bin/env python3
"""サイト更新を IndexNow（Bing・Yandex・Naver・Seznam 等）に通知するスクリプト。

GitHub Actions で dist/ を push した後に実行する。Cloudflare Pages への反映を待ってから通知するため、
dist/index.html の「最終更新」表記が本番ページに出るまで待機する（最大 WAIT_SECONDS 秒）。
通知に失敗してもサイト更新自体には影響しないよう、常に終了コード 0 で終わる。
"""

from __future__ import annotations

import html
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from fetch_and_build import INDEXNOW_KEY, JST, OUTPUT_PATH, SITE_URL, USER_AGENT

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
WAIT_SECONDS = 600
POLL_INTERVAL = 20

RESULT_MESSAGES = {
    200: "受け付けられました",
    202: "受け付けられました（キーの確認中）",
    400: "リクエスト形式の誤り",
    403: "キーが無効（キーファイルが公開されていない可能性）",
    422: "URL がキーのホストと一致しない",
    429: "送信が多すぎます（時間をおいて再送）",
}


def wait_for_deploy() -> bool:
    """本番ページに今回ビルドした「最終更新」表記が出るまで待つ。"""
    match = re.search(r"最終更新: [^<(]+", Path(OUTPUT_PATH).read_text(encoding="utf-8"))
    if not match:
        return True
    marker = match.group(0).strip()
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        try:
            resp = requests.get(
                SITE_URL, params={"_": int(time.time())}, headers={"User-Agent": USER_AGENT}, timeout=15
            )
            if marker in resp.text:
                print(f"[INFO] 本番への反映を確認しました（{marker}）")
                return True
        except requests.RequestException:
            pass
        time.sleep(POLL_INTERVAL)
    print(f"[WARN] {WAIT_SECONDS} 秒待っても本番に反映されませんでした。通知を見送ります", file=sys.stderr)
    return False


def notify(urls: list[str]) -> None:
    host = re.sub(r"^https?://", "", SITE_URL).split("/")[0]
    payload = {
        "host": host,
        "key": INDEXNOW_KEY,
        "keyLocation": f"{SITE_URL}{INDEXNOW_KEY}.txt",
        "urlList": urls,
    }
    try:
        resp = requests.post(
            INDEXNOW_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"::warning title=IndexNow::通信失敗 ({type(exc).__name__})")
        return
    msg = RESULT_MESSAGES.get(resp.status_code, resp.text[:200])
    level = "notice" if resp.status_code in (200, 202) else "warning"
    print(f"::{level} title=IndexNow::HTTP {resp.status_code} {msg}（{', '.join(urls)}）")


def changed_urls() -> list[str]:
    """sitemap.xml のうち、更新日（lastmod）が今日の URL を返す（トップページは毎日更新）。"""
    today = datetime.now(JST).date().isoformat()
    try:
        sitemap = (Path(OUTPUT_PATH).parent / "sitemap.xml").read_text(encoding="utf-8")
    except OSError:
        return [SITE_URL]
    urls = [
        html.unescape(loc)
        for loc, mod in re.findall(r"<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>", sitemap)
        if mod == today
    ]
    return urls or [SITE_URL]


def main() -> int:
    if "--no-wait" in sys.argv or wait_for_deploy():
        notify(changed_urls())
    return 0


if __name__ == "__main__":
    sys.exit(main())
