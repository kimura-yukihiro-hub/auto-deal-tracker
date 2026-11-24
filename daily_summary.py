"""トップの「今日の見張り報告」に載せる、その日のセールの傾向のまとめ文（2〜3文）を作る。

ほかのサイトの見出しを並べるだけでなく、このサイトにしかない文章を毎日1つ置くため（Google の評価と、読む人の分かりやすさ）。
- 1 日 1 回だけ Gemini で作り、data/daily_summary.json にしまう（同じ日の 2 回目以降の更新では作り直さない）
- 見出しにない数字・ほかのサイトにない評価は書かせない。コードでも確かめ、だめなら載せない（まとめ文なしで表示する）
- GEMINI_API_KEY がないとき・失敗したときも、サイトの更新は止めない
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
CACHE = Path(__file__).resolve().parent / "data" / "daily_summary.json"
MAX_ITEMS = 15
BANNED = re.compile(r"おすすめ|オススメ|最安|絶対|必見|見逃せない|今すぐ|お見逃しなく|No\.?\s?1|一番")

SYSTEM = """あなたはセール情報サイト「セールの見張り番」の編集アシスタントです。
その日に見つかったセールの見出しの一覧から、トップページに載せる短いまとめ文を書きます。
- 2〜3文、合わせて120字以内。です・ます調。
- その日のセールの傾向（どんな商品・店・メーカーのセールが多いか、期限の近いものがあるか）を、一覧にある事実だけで書く。
- 一覧にない数字・商品名・店名は書かない。値引きの大きさを書くときは、見出しに書かれた数字をそのまま使う。
- 「おすすめ」「お得」「見逃せない」「今すぐ」などのあおる言葉、感想、体験談は書かない。
- 次の JSON だけを出力する: {"summary": "まとめ文"}"""


def _key(sales: list[dict]) -> str:
    return "|".join(sorted(s["title"] for s in sales))[:4000]


def _load() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def check(text: str, sales: list[dict]) -> str | None:
    """載せてよいかを確かめる。だめならその理由。"""
    if not 30 <= len(text) <= 160:
        return f"長さが合わない（{len(text)}字）"
    if BANNED.search(text):
        return f"あおる言葉がある（{BANNED.search(text).group(0)}）"
    source = " ".join(s["title"] + " " + s.get("source", "") for s in sales)
    src_nums = set(re.findall(r"\d+(?:[,.]\d+)?", source.replace(",", "")))
    for n in re.findall(r"\d+(?:[,.]\d+)?", text.replace(",", "")):
        if n not in src_nums and not (n.isdigit() and int(n) == len(sales)):
            return f"見出しにない数字がある（{n}）"
    return None


def get_summary(sales: list[dict], now: datetime | None = None) -> str:
    """その日のまとめ文。作れない・載せられないときは空文字。sales は {"title", "source"} の並び。"""
    now = now or datetime.now(JST)
    today = now.date().isoformat()
    sales = sales[:MAX_ITEMS]
    cache = _load()
    if cache.get("date") == today:   # 1 日 1 回だけ作る
        return cache.get("text", "")
    if len(sales) < 2:
        return ""
    try:
        from ai_draft import GEMINI_API_KEY, call_gemini, parse_json_object
        if not GEMINI_API_KEY:
            return ""
        listing = "\n".join(f"- {s['title']}（{s.get('source', '')}）" for s in sales)
        text = parse_json_object(call_gemini(SYSTEM, f"今日（{now:%m月%d日}）のセールの見出し {len(sales)}件:\n{listing}",
                                              temperature=0.4, retries=2)).get("summary", "")
        text = re.sub(r"\s+", " ", str(text)).strip()
        why = check(text, sales)
        if why:
            print(f"[INFO] 今日のまとめ文は載せません（{why}）: {text[:80]}")
            text = ""
    except Exception as e:  # まとめ文がなくても、サイトの更新は続ける
        print(f"[INFO] 今日のまとめ文を作れませんでした（{type(e).__name__}: {str(e)[:120]}）")
        return ""
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"date": today, "key": _key(sales), "text": text}, ensure_ascii=False, indent=1) + "\n",
                     encoding="utf-8")
    return text
