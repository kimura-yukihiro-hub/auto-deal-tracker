#!/usr/bin/env python3
"""Gemini API で記事の「下書き」を作り、GitHub Issue で知らせるスクリプト。

使い方（GitHub Actions から呼び出す）:
    python ai_draft.py            # 日曜日なら下書きを作成し、作成したパスを GITHUB_OUTPUT に出力
    python ai_draft.py --notify P # 作成した下書き P について GitHub Issue を作成

環境変数:
    GEMINI_API_KEY : Google AI Studio の API キー（未設定なら何もしない）
    GEMINI_MODEL   : 使うモデル（既定: gemini-3.6-flash）
    FORCE_AI_DRAFT : "1" なら曜日に関係なく作成（動作確認用）

方針:
- 下書きは draft: true（非公開）で articles/ai-draft-YYYY-MM-DD.md に保存する。人が確認して公開する
- AI には data/deals_log.json の直近7日分（公開ニュースのタイトル・要約・URL）だけを渡す
- 渡していない URL へのリンクは自動で外す（AI が URL を作り出すのを防ぐ）
- 失敗してもサイトの更新には影響しない（常に終了コード 0）
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
ARTICLES_DIR = BASE_DIR / "articles"
DEALS_LOG_PATH = BASE_DIR / "data" / "deals_log.json"
FEATURED_PATH = BASE_DIR / "data" / "featured.json"
JST = timezone(timedelta(hours=9))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.6-flash"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
DRAFT_WEEKDAY = 6  # 日曜日
MAX_INPUT_ITEMS = 25
TIMEOUT = 120

SYSTEM_PROMPT = """あなたはガジェット・セール情報サイト「Auto Deal Tracker」の編集アシスタントです。
編集者が仕上げるための記事の「下書き」を日本語で作成します。

厳守するルール:
- 事実は、与えられたデータ（ニュースのタイトル・要約・URL）に書かれていることだけを使う。
- データにない価格・割引率・仕様・発売日・セール期間を推測で書かない。数字はデータにあるものだけ。
- リンクには、与えられたデータの URL だけを使う。URL を作り出したり変えたりしない。
- 自分が商品を使ったかのような体験談や感想は書かない。
  代わりに、編集者が体験や意見を書き足す場所に「【編集者メモ：ここに実際の使用感や選んだ理由を追記】」と書く。
- 各商品・情報の紹介には、出典メディア名とリンクを付ける。
- 誇張表現（「絶対」「最強」「必ず得する」など）を使わない。
- 本文は Markdown。見出しは ## と ### を使い、# は使わない。HTML タグは使わない。"""

USER_PROMPT_TEMPLATE = """以下は {start} から {end} までに主要テックメディアで報じられた、ガジェットのセール・値下げ関連ニュースです。

{items}

この中から読者の関心が高そうなものを3〜5件選び、「今週注目のガジェット・セール」という趣旨の記事の下書きを作ってください。

構成:
1. 導入（2〜3文）
2. 選んだ各ニュースについて ### 見出し + 紹介文（データに基づく事実のみ）+ 「【編集者メモ：…】」の空欄 + 出典リンク
3. 購入前に価格・在庫・セール期間をリンク先で確認するよう促す締めの段落

次の JSON 形式だけを出力してください（前後に説明文を付けない）:
{{"title": "記事タイトル（40字以内）", "description": "記事の紹介文（80〜120字）", "tags": ["タグ1", "タグ2"], "body": "本文（Markdown）"}}"""


# ---------------------------------------------------------------------------
# 入力データ
# ---------------------------------------------------------------------------

def load_week_entries(today) -> list[dict]:
    try:
        log = json.loads(DEALS_LOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    start = (today - timedelta(days=6)).isoformat()
    entries = [e for e in log if isinstance(e, dict) and start <= e.get("date", "") <= today.isoformat()]
    return entries[:MAX_INPUT_ITEMS]


def format_items(entries: list[dict]) -> str:
    lines = []
    for i, e in enumerate(entries, 1):
        lines.append(
            f"[{i}] タイトル: {e.get('title', '')}\n"
            f"    出典: {e.get('source', '')} / 日付: {e.get('date', '')}\n"
            f"    要約: {e.get('summary', '')}\n"
            f"    URL: {e.get('link', '')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini API
# ---------------------------------------------------------------------------

def _extract_text(data) -> str:
    """generateContent / Interactions API どちらの応答からもテキストを取り出す。"""
    if isinstance(data, list):  # ストリーミング形式で返った場合
        return "".join(_extract_text(d) for d in data)
    if not isinstance(data, dict):
        return ""
    for cand in data.get("candidates", []) or []:
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if text:
            return text
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    for step in reversed(data.get("steps", []) or data.get("outputs", []) or []):
        contents = step.get("content", []) if isinstance(step, dict) else []
        if isinstance(contents, dict):
            contents = [contents]
        text = "".join(c.get("text", "") for c in contents if isinstance(c, dict) and c.get("type", "text") == "text")
        if text:
            return text
    return ""


def call_gemini(system: str, prompt: str) -> str:
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    # 1) 従来の generateContent
    resp = requests.post(
        f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent",
        headers=headers,
        json={
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 8192},
        },
        timeout=TIMEOUT,
    )
    if resp.status_code == 200:
        return _extract_text(resp.json())
    if resp.status_code not in (404, 405):
        raise RuntimeError(f"Gemini API エラー HTTP {resp.status_code}: {resp.text[:300]}")
    # 2) generateContent が使えない場合は Interactions API
    resp = requests.post(
        f"{GEMINI_BASE}/interactions",
        headers={**headers, "Api-Revision": "2026-05-20"},
        json={
            "model": GEMINI_MODEL,
            "system_instruction": system,
            "input": prompt,
            "generation_config": {"max_output_tokens": 8192},
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API エラー HTTP {resp.status_code}: {resp.text[:300]}")
    return _extract_text(resp.json())


def parse_response(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("JSON が見つかりません")
    data = json.loads(text[start : end + 1])
    if not data.get("title") or not data.get("body"):
        raise ValueError("title または body がありません")
    return data


# ---------------------------------------------------------------------------
# 安全対策
# ---------------------------------------------------------------------------

def sanitize_body(body: str, allowed_urls: set[str]) -> tuple[str, list[str]]:
    """渡していない URL へのリンクを外し、HTML タグと # 見出しを無害化する。"""
    removed: list[str] = []

    def fix_link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2).strip()
        if url in allowed_urls:
            return m.group(0)
        removed.append(url)
        return label

    body = re.sub(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", fix_link, body)
    # リンク記法の外に裸で書かれた URL も、許可リストにないものは削除
    def fix_bare(m: re.Match) -> str:
        url = m.group(0)
        if url in allowed_urls:
            return url
        removed.append(url)
        return ""

    body = re.sub(r"(?<!\()https?://[^\s)<>\]]+", fix_bare, body)
    body = re.sub(r"<[^>]+>", "", body)  # HTML タグは使わせない
    body = re.sub(r"(?m)^#(?!#)\s*", "## ", body)  # # 見出しは ## に下げる
    return body.strip(), removed


def _yaml_value(text: str) -> str:
    return re.sub(r"\s*:\s*", "：", re.sub(r"\s+", " ", str(text))).strip()


def build_draft_file(data: dict, today, removed: list[str]) -> str:
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    tags = [_yaml_value(t) for t in tags if str(t).strip()][:5] or ["セール"]
    note = (
        "<!--\n"
        "この下書きは AI（Gemini）が自動で作成しました。公開前に必ず確認してください:\n"
        "  1. 内容が元記事と合っているか（各リンク先で確認）\n"
        "  2. 【編集者メモ：…】の部分に、自分の言葉で感想や選んだ理由を書く（不要なら削除）\n"
        "  3. 公開するときは上の draft: true の行を削除する（date も公開日に合わせる）\n"
        "  4. ファイル名がそのまま URL になるので、内容に合った英小文字の名前に変えるとわかりやすい\n"
        "     （例: articles/charger-sale-picks.md）\n"
        "  5. このコメント全体も削除してかまいません\n"
        + (f"  ※ データにない URL へのリンクを {len(removed)} 件、自動で外しました\n" if removed else "")
        + "-->\n\n"
    )
    return (
        "---\n"
        f"title: {_yaml_value(data['title'])}\n"
        f"date: {today.isoformat()}\n"
        f"description: {_yaml_value(data.get('description', ''))}\n"
        f"tags: {', '.join(tags)}\n"
        "draft: true\n"
        "---\n\n" + note + data["body"].strip() + "\n"
    )


# ---------------------------------------------------------------------------
# GitHub への通知
# ---------------------------------------------------------------------------

def annotate(level: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title=AI下書き::{message}")
    else:
        print(f"[{level.upper()}] {message}")


def set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def notify(draft_path: str) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    if not (token and repo):
        annotate("notice", "GitHub 外での実行のため Issue は作成しません")
        return
    rel = Path(draft_path).as_posix()
    title_match = re.search(r"(?m)^title:\s*(.+)$", Path(draft_path).read_text(encoding="utf-8"))
    title = title_match.group(1).strip() if title_match else rel
    body = (
        f"AI が記事の下書きを作成しました。\n\n"
        f"**{title}**\n\n"
        f"- ファイル: [{rel}]({server}/{repo}/blob/main/{rel})\n"
        f"- 編集する: [GitHub で編集]({server}/{repo}/edit/main/{rel})\n\n"
        "### 公開までのチェックリスト\n"
        "- [ ] 内容が元記事と合っているか、各リンク先で確認した\n"
        "- [ ] 【編集者メモ：…】に自分の感想・選んだ理由を書いた（または削除した）\n"
        "- [ ] `draft: true` の行を削除し、`date` を公開日にした\n"
        "- [ ] （任意）ファイル名を内容に合った英小文字の名前に変えた（URL になります）\n\n"
        "公開しない場合は、このファイルを削除してください。確認が終わったらこの Issue を閉じてください。"
    )
    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": f"📝 AI下書きの確認: {title}", "body": body},
        timeout=30,
    )
    if resp.status_code == 201:
        annotate("notice", f"Issue を作成しました: {resp.json().get('html_url', '')}")
    else:
        annotate("warning", f"Issue の作成に失敗しました HTTP {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def create_draft() -> None:
    today = datetime.now(JST).date()
    if today.weekday() != DRAFT_WEEKDAY and os.environ.get("FORCE_AI_DRAFT") != "1":
        return
    if not GEMINI_API_KEY:
        annotate("notice", "GEMINI_API_KEY が未設定のため下書きを作成しません")
        return
    path = ARTICLES_DIR / f"ai-draft-{today.isoformat()}.md"
    if path.exists():
        print(f"[INFO] {path.name} は作成済みのためスキップ")
        return
    entries = load_week_entries(today)
    if len(entries) < 3:
        annotate("notice", f"今週のセール記事が {len(entries)} 件と少ないため下書きを作成しません")
        return

    prompt = USER_PROMPT_TEMPLATE.format(
        start=(today - timedelta(days=6)).isoformat(), end=today.isoformat(), items=format_items(entries)
    )
    data = parse_response(call_gemini(SYSTEM_PROMPT, prompt))
    allowed = {e.get("link", "") for e in entries if e.get("link")}
    body, removed = sanitize_body(str(data["body"]), allowed)
    data["body"] = body
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(build_draft_file(data, today, removed), encoding="utf-8")
    rel = path.relative_to(BASE_DIR).as_posix()
    annotate("notice", f"下書きを作成しました: {rel}（モデル: {GEMINI_MODEL}、外したリンク: {len(removed)} 件）")
    set_output("draft", rel)


def main() -> int:
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "--notify":
            notify(sys.argv[2])
        else:
            create_draft()
    except Exception as exc:  # 失敗してもサイト更新は止めない
        annotate("warning", f"失敗しました: {type(exc).__name__}: {str(exc)[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
