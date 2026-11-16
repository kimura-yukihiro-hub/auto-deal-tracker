#!/usr/bin/env python3
"""Gemini API で「選び方の記事」の下書きを作り、GitHub Issue で知らせるスクリプト。

使い方（GitHub Actions から呼び出す）:
    python ai_draft.py            # 日曜日なら下書きを作成し、作成したパスを GITHUB_OUTPUT に出力
    python ai_draft.py --notify P # 作成した下書き P について GitHub Issue を作成

環境変数:
    GEMINI_API_KEY : Google AI Studio の API キー（未設定なら何もしない）
    GEMINI_MODEL   : 使うモデル（既定: gemini-3.6-flash）
    FORCE_AI_DRAFT : "1" なら曜日に関係なく作成（動作確認用）

方針:
- その週のセールで多かった製品の「選び方」記事を作る（週刊セールまとめと内容がかぶらないように）
- 下書きは draft: true（非公開）で articles/<AI が付けた英小文字の名前>.md に保存する。人が確認して公開する
- AI には data/deals_log.json の直近7日分（公開ニュースのタイトル・要約・URL）と、既存記事の一覧を渡す
- 一般的な知識で書いた部分のうち、数字やルールなど確認が必要なところには【要確認：…】を付けさせる
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

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EDITOR_MEMO = "【編集者メモ：選んだ理由や、自分が重視したいポイントを一言追記】"

SYSTEM_PROMPT = """あなたはセール情報サイト「セールの見張り番」の編集アシスタントです。
編集者が仕上げるための「製品の選び方」記事の下書きを日本語で作成します。

読者: スマホ・パソコン・家電を少しでもお得に買いたい人。機械に詳しくない人や、仕事でパソコンを使う人が中心。
- 専門用語はなるべく使わず、使う場合は短く言い換えや説明を添える（例: 「PD対応（スマホやノートPCを速く充電できる規格）」）。
- 「どんな人に向いているか」「何が便利か」を、生活や仕事の場面に結びつけて書く。

厳守するルール:
- 個別の商品の価格・割引率・仕様・セール期間は、与えられたニュースのデータに書かれていることだけを使う。推測で書かない。
- 選び方の説明には一般的な知識を使ってよい。ただし、具体的な数字の目安・法律や規格のルール・安全に関わる情報など、
  間違えると読者が困る内容には、その文の直後に「【要確認：何を確認すべきか】」と書き、編集者が確認できるようにする。
  自信のない内容は書かない。
- リンクには、与えられたニュースの URL と、既存記事のパス（/articles/〜/）だけを使う。URL を作り出したり変えたりしない。
- 自分が商品を使ったかのような体験談や感想は書かない。
  代わりに、編集者が意見を書き足す場所に「""" + EDITOR_MEMO + """」と書く（記事全体で2〜3か所）。
- セール情報を紹介するときは、出典メディア名とリンクを付ける。
- 誇張表現（「絶対」「最強」「必ず得する」など）を使わない。
- 本文は Markdown。見出しは ## と ### を使い、# は使わない。HTML タグは使わない。"""

USER_PROMPT_TEMPLATE = """以下は {start} から {end} までに主要IT・家電メディアで報じられた、スマホ・パソコン・家電のセール・値下げ関連ニュースです。

{items}

当サイトにすでにある記事（この製品の選び方はもう書いてあるので、同じテーマは選ばない）:
{existing}

上のニュースの中で、セールの対象として多く出てきた製品の種類（例: ロボット掃除機、ノートパソコン、ワイヤレスイヤホン）を1つ選び、
その製品の「選び方」を、詳しくない人にもわかるように説明する記事の下書きを作ってください。
セール情報の一覧（週刊まとめ）は別の記事で公開しているので、セールの紹介が中心の記事にはしないでください。

構成:
1. 導入（2〜3文）+ 「""" + EDITOR_MEMO + """」
2. 「選び方1：〜」のような ## 見出しで、選ぶときのポイントを3〜5個（向いている人や使う場面に結びつける。表を1つ使うとよい）
3. 「買う前のチェックポイント」（番号付きリスト）
4. 「今週のセールで見つけた〇〇」: 上のニュースのうち、選んだ製品のセール情報を1〜3件、出典リンク付きで紹介し、
   その後に「""" + EDITOR_MEMO + """」
5. 「まとめ」（箇条書き）と、購入前に価格・在庫・セール期間をリンク先で確認するよう促す一文
関係のある既存記事があれば、本文中でそのパスにリンクしてよい。

次の JSON 形式だけを出力してください（前後に説明文を付けない）:
{{"title": "記事タイトル（〇〇の選び方 で始まり、40字以内）", "slug": "URL 用の英小文字とハイフンの名前（例: robot-vacuum-guide）", "description": "記事の紹介文（80〜120字）", "tags": ["タグ1", "タグ2"], "body": "本文（Markdown）"}}"""


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


def load_existing_articles() -> list[dict]:
    """既存の記事（下書きも含む）の名前・タイトル・タグ。同じテーマの下書きを作らないために AI に渡す。"""
    articles = []
    for path in sorted(ARTICLES_DIR.glob("*.md")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        title = re.search(r"(?m)^title:\s*(.+)$", text)
        tags = re.search(r"(?m)^tags:\s*(.+)$", text)
        articles.append({
            "slug": path.stem,
            "title": title.group(1).strip() if title else path.stem,
            "tags": tags.group(1).strip() if tags else "",
        })
    return articles


def format_existing(articles: list[dict]) -> str:
    return "\n".join(f"- {a['title']}（パス: /articles/{a['slug']}/、タグ: {a['tags']}）" for a in articles) or "（なし）"


def choose_slug(raw, today) -> str:
    """AI が付けた名前を検証し、使えなければ日付入りの名前にする。既存のファイルとは重ならないようにする。"""
    slug = str(raw or "").strip().lower()
    if not SLUG_RE.match(slug) or len(slug) > 50:
        slug = f"guide-draft-{today.isoformat()}"
    if (ARTICLES_DIR / f"{slug}.md").exists():
        slug = f"{slug}-{today.isoformat()}"
    return slug


def already_drafted(today) -> bool:
    """今日の下書きを作成済みか（下書きの公開前メモに入れた目印で判断する）。"""
    marker = f"ai-draft: {today.isoformat()}"
    return any(marker in p.read_text(encoding="utf-8") for p in ARTICLES_DIR.glob("*.md"))


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
        f"ai-draft: {today.isoformat()}\n"
        "この下書きは AI（Gemini）が自動で作成しました。公開前にやること（終わったらこのメモごと削除してください）:\n"
        "  1. 【要確認：…】の部分を調べて、正しければ【要確認：…】だけを消す（間違いや自信のない文は直すか削除）\n"
        "  2. セール情報の部分が元記事と合っているか、各リンク先で確認する\n"
        "  3. 【編集者メモ：…】の部分を、自分の言葉で書き換える（使ったことのない商品の感想は書かない）\n"
        "  4. 上の「draft: true」の行を削除する ← これを消さないと公開されません\n"
        "  5. date を公開する日に変える\n"
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
        "- [ ] 【要確認：…】の部分を調べて、正しいことを確かめた（間違いは直した）\n"
        "- [ ] セール情報の部分が元記事と合っているか、各リンク先で確認した\n"
        "- [ ] 【編集者メモ：…】を自分の言葉で書き換えた（または削除した）\n"
        "- [ ] `draft: true` の行と、一番上の公開前メモ（`<!-- … -->`）を削除し、`date` を公開日にした\n\n"
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
    if already_drafted(today):
        print("[INFO] 今日の下書きは作成済みのためスキップ")
        return
    entries = load_week_entries(today)
    if len(entries) < 3:
        annotate("notice", f"今週のセール記事が {len(entries)} 件と少ないため下書きを作成しません")
        return

    existing = load_existing_articles()
    prompt = USER_PROMPT_TEMPLATE.format(
        start=(today - timedelta(days=6)).isoformat(),
        end=today.isoformat(),
        items=format_items(entries),
        existing=format_existing(existing),
    )
    data = parse_response(call_gemini(SYSTEM_PROMPT, prompt))
    allowed = {e.get("link", "") for e in entries if e.get("link")}
    allowed |= {f"/articles/{a['slug']}/" for a in existing}
    path = ARTICLES_DIR / f"{choose_slug(data.get('slug'), today)}.md"
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
