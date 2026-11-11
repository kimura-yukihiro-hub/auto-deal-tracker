import json
import re
from datetime import date, datetime

import fetch_and_build as fb
from articles import load_articles
from digest import maybe_write_weekly_digest, update_log

SUNDAY = date(2026, 11, 15)


def items_for_week():
    J = fb.JST
    return [
        fb.Item("充電器がセールで33％オフ <速報> & [限定]", "要約A", "https://ex.com/1", datetime(2026, 11, 9, 10, tzinfo=J), "ITmedia Mobile", "ガジェット"),
        fb.Item("【本日みつけたお買い得品】*約2万円オフ*のKindle", "要約B", "https://ex.com/2", datetime(2026, 11, 10, 9, tzinfo=J), "PC Watch", "ガジェット"),
        fb.Item("新しいCPUを発表", "要約C", "https://ex.com/3", datetime(2026, 11, 10, 9, tzinfo=J), "PC Watch", "ガジェット"),
        fb.Item("マウスが20%OFFのタイムセール", "要約D", "https://ex.com/4", datetime(2026, 11, 11, 9, tzinfo=J), "GetNavi web", "ガジェット"),
        fb.Item("古い記事のセール", "要約E", "https://ex.com/5", datetime(2026, 10, 1, 9, tzinfo=J), "GetNavi web", "ガジェット"),
    ]


def test_update_log_keeps_only_recent_sale_items_without_duplicates(tmp_path):
    path = tmp_path / "log.json"
    log = update_log(items_for_week(), path, SUNDAY)
    assert [e["link"] for e in log] == ["https://ex.com/4", "https://ex.com/2", "https://ex.com/1"]
    log = update_log(items_for_week(), path, SUNDAY)
    assert len(log) == 3
    assert json.loads(path.read_text(encoding="utf-8")) == log


def test_weekly_digest_only_on_sunday_and_once(tmp_path):
    log = update_log(items_for_week(), tmp_path / "log.json", SUNDAY)
    out = tmp_path / "articles"
    assert maybe_write_weekly_digest(log, [], out, date(2026, 11, 14)) is None
    path = maybe_write_weekly_digest(log, [], out, SUNDAY)
    assert path is not None and path.name == "weekly-deals-2026-11-15.md"
    assert maybe_write_weekly_digest(log, [], out, SUNDAY) is None
    assert maybe_write_weekly_digest(log[:2], [], tmp_path / "few", SUNDAY) is None


def test_weekly_digest_renders_safely(tmp_path):
    log = update_log(items_for_week(), tmp_path / "log.json", SUNDAY)
    picks = [fb.Pick("Anker 充電器", "定番", "https://search.rakuten.co.jp/search/mall/Anker/", True)]
    maybe_write_weekly_digest(log, picks, tmp_path / "articles", SUNDAY)
    article = load_articles(tmp_path / "articles", "auto-deal-tracker.pages.dev", today=SUNDAY)[0]
    body = article.body_html
    assert article.has_affiliate  # 編集部ピックアップの楽天リンク
    assert "充電器がセールで33％オフ &lt;速報&gt; &amp; [限定]" in body
    assert "*約2万円オフ*" in body
    assert "\\" not in body
    assert "<li>" in body and not re.search(r"<li>(?:(?!</li>).)*<h3", body, re.S)
