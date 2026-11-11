"""週1回の「セール情報まとめ」記事を自動で作る。

- 毎朝の更新で、ニュースのうちセール・値下げ関連の記事を data/deals_log.json に記録する
  （RSS は最新の数件しか配信しないため、1週間分を自分で貯めておく）
- 日曜日の朝の更新で、直近7日分の記録から articles/weekly-deals-YYYY-MM-DD.md を作る
  （同じ日の2回目以降の実行では作り直さない。作成後は手で編集してもよい）
- 本文の楽天市場リンク（編集部ピックアップ）は、記事の変換時にアフィリエイトリンクになる
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

# タイトルにこれらの語を含む記事を「セール・値下げ情報」とみなす
SALE_KEYWORDS = re.compile(
    r"セール|SALE|Sale|値下げ|値引き|割引|特価|最安|お買い得|安売り|激安|"
    r"\d+\s*[%％]\s*(?:OFF|off|オフ)|\d+\s*割引|オフで|ポイント\d+倍|クーポン|タイムセール|ブラックフライデー|プライムデー"
)
LOG_RETENTION_DAYS = 21
DIGEST_WEEKDAY = 6  # 日曜日（月曜=0）
DIGEST_DAYS = 7
DIGEST_MIN_ITEMS = 3
DIGEST_MAX_ITEMS = 25
SUMMARY_LENGTH = 80
WEEKDAYS_JA = "月火水木金土日"


def is_sale_title(title: str) -> bool:
    return bool(SALE_KEYWORDS.search(title))


def _load_log(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def update_log(items, path: Path, today: date) -> list[dict]:
    """今回取得したニュースのうちセール関連を記録に追加し、古い記録を削除して保存する。"""
    log = _load_log(path)
    known = {entry.get("link") for entry in log}
    cutoff = (today - timedelta(days=LOG_RETENTION_DAYS)).isoformat()
    added = 0
    for item in items:
        if not item.link or item.link in known or not is_sale_title(item.title):
            continue
        published = item.published.date() if item.published else today
        if published.isoformat() < cutoff:
            continue
        log.append(
            {
                "title": item.title,
                "link": item.link,
                "source": item.source,
                "summary": item.summary,
                "date": published.isoformat(),
            }
        )
        known.add(item.link)
        added += 1
    log =[entry for entry in log if entry.get("date", "") >= cutoff]
    log.sort(key=lambda e: (e.get("date", ""), e.get("title", "")), reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[INFO] セール記録: {added} 件追加（保存中 {len(log)} 件）")
    return log


def _md_text(text: str) -> str:
    """Markdown の記号として解釈されないようにエスケープする（Python-Markdown がエスケープできる文字のみ）。"""
    text = re.sub(r"([\\`*_\[\]>#])", r"\\\1", text)
    return text.replace("&", "&amp;").replace("<", "&lt;")


def _short(text: str, length: int = SUMMARY_LENGTH) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= length else text[:length].rstrip() + "…"


def _jp_date(d: date) -> str:
    return f"{d.month}月{d.day}日（{WEEKDAYS_JA[d.weekday()]}）"


def build_digest_markdown(entries: list[dict], picks, start: date, end: date) -> str:
    period = f"{start.month}/{start.day}〜{end.month}/{end.day}"
    lines = [
        "---",
        f"title: 今週のガジェット・セール情報まとめ（{period}）",
        f"date: {end.isoformat()}",
        f"description: {period}に主要テックメディアで報じられたガジェットのセール・値下げ情報 {len(entries)} 件をまとめました。",
        "tags: セールまとめ, 週刊",
        "---",
        "",
        f"{start.month}月{start.day}日から{end.month}月{end.day}日までの1週間に、"
        "主要テック・ガジェット系メディアで報じられたセール・値下げ情報をまとめました。",
        "",
        "> この記事は、当サイトが毎日収集しているニュースのうち、タイトルにセール・値下げ関連の語を含むものを"
        "自動で集計して作成しています。価格やセール期間は各記事の掲載時点のものです。"
        "購入前に必ずリンク先で最新の情報を確認してください。",
        "",
    ]
    manual_picks = [p for p in picks if getattr(p, "manual", False)]
    if manual_picks:
        lines += ["## 編集部ピックアップ", ""]
        for p in manual_picks:
            comment = f"：{_md_text(_short(p.comment, 100))}" if p.comment else ""
            lines.append(f"- [{_md_text(p.title)}]({p.url}){comment}")
        lines.append("")

    lines += ["## 今週のセール・値下げ情報", ""]
    current = None
    for entry in entries:
        d = date.fromisoformat(entry["date"])
        if d != current:
            if current is not None:
                lines.append("")
            current = d
            lines += [f"### {_jp_date(d)}", ""]
        summary = _short(entry.get("summary", ""))
        line = f"- **[{_md_text(entry['title'])}]({entry['link']})**（{_md_text(entry['source'])}）"
        if summary and summary != "要約はありません。":
            # 行末の空白2つで改行し、要約を次の行に表示する
            lines += [line + "  ", f"  {_md_text(summary)}"]
        else:
            lines.append(line)
    lines += [
        "",
        "## 情報元",
        "",
        "各記事の内容・著作権は、それぞれの配信元メディアに帰属します。"
        "詳しい内容は各リンク先の元記事をご覧ください。",
        "",
    ]
    return "\n".join(lines)


def maybe_write_weekly_digest(log: list[dict], picks, articles_dir: Path, today: date, force: bool = False) -> Path | None:
    """日曜日（または force 指定時）に、直近7日分のまとめ記事を作る。作成済みなら何もしない。"""
    if today.weekday() != DIGEST_WEEKDAY and not force:
        return None
    path = articles_dir / f"weekly-deals-{today.isoformat()}.md"
    if path.exists():
        return None
    start = today - timedelta(days=DIGEST_DAYS - 1)
    entries = [e for e in log if start.isoformat() <= e.get("date", "") <= today.isoformat()]
    entries = entries[:DIGEST_MAX_ITEMS]
    if len(entries) < DIGEST_MIN_ITEMS:
        print(f"[INFO] 週刊まとめ: セール記事が {len(entries)} 件と少ないため今週は作成しません")
        return None
    period_start = min(date.fromisoformat(e["date"]) for e in entries)
    articles_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(build_digest_markdown(entries, picks, max(start, period_start), today), encoding="utf-8")
    print(f"[INFO] 週刊まとめ記事を作成しました: {path.name}（{len(entries)} 件）")
    return path


if __name__ == "__main__":
    print("このモジュールは fetch_and_build.py から呼び出して使います。", file=sys.stderr)
