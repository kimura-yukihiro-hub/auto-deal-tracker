import json
from datetime import date, datetime

import ai_draft
import notify_indexnow
from fetch_and_build import JST, SITE_URL

ALLOWED = {"https://www.itmedia.co.jp/mobile/articles/1.html"}


def test_sanitize_body_removes_unknown_links_and_html():
    body = (
        "# 大見出し\n[元記事](https://www.itmedia.co.jp/mobile/articles/1.html)\n"
        "[偽ストア](https://fake.example.com/deal) と https://another-fake.example.net/x <script>alert(1)</script>"
    )
    cleaned, removed = ai_draft.sanitize_body(body, ALLOWED)
    assert "(https://www.itmedia.co.jp/mobile/articles/1.html)" in cleaned
    assert "fake.example.com" not in cleaned and "another-fake" not in cleaned
    assert "偽ストア" in cleaned  # リンクだけ外して文字は残す
    assert "<script" not in cleaned
    assert cleaned.startswith("## 大見出し")
    assert len(removed) == 2


def test_parse_response_handles_code_fence():
    data = {"title": "t", "description": "d", "tags": ["a"], "body": "b"}
    assert ai_draft.parse_response("```json\n" + json.dumps(data) + "\n```") == data


def test_extract_text_from_both_api_shapes():
    gen = {"candidates": [{"content": {"parts": [{"text": "think", "thought": True}, {"text": "答え"}]}}]}
    inter = {"steps": [{"content": [{"type": "text", "text": "答え2"}]}]}
    assert ai_draft._extract_text(gen) == "答え"
    assert ai_draft._extract_text(inter) == "答え2"
    assert ai_draft._extract_text({"output_text": "答え3"}) == "答え3"


def test_draft_file_is_draft_and_comment_closes_once():
    text = ai_draft.build_draft_file({"title": "今週: 注目", "body": "本文", "tags": "a, b"}, date(2026, 11, 15), [])
    assert "\ndraft: true\n" in text
    assert "title: 今週：注目\n" in text
    assert text.count("<!--") == 1 and text.count("-->") == 1


def test_indexnow_targets_today_lastmod(tmp_path, monkeypatch):
    today = datetime.now(JST).date().isoformat()
    (tmp_path / "sitemap.xml").write_text(
        "<urlset>"
        f"<url><loc>{SITE_URL}</loc><lastmod>{today}</lastmod></url>"
        f"<url><loc>{SITE_URL}articles/old/</loc><lastmod>2000-01-01</lastmod></url>"
        f"<url><loc>{SITE_URL}articles/new/</loc><lastmod>{today}</lastmod></url>"
        "</urlset>",
        encoding="utf-8",
    )
    monkeypatch.setattr(notify_indexnow, "OUTPUT_PATH", tmp_path / "index.html")
    assert notify_indexnow.changed_urls() == [SITE_URL, f"{SITE_URL}articles/new/"]


def test_guide_draft_slug_marker_and_prompt(tmp_path, monkeypatch):
    """選び方の下書き: 名前の検証、同じ日の二重作成防止、既存記事の一覧をプロンプトに入れる。"""
    monkeypatch.setattr(ai_draft, "ARTICLES_DIR", tmp_path)
    today = date(2026, 11, 22)
    (tmp_path / "mobile-battery-guide.md").write_text(
        "---\ntitle: モバイルバッテリーの選び方\ntags: a, b\n---\n", encoding="utf-8"
    )
    assert ai_draft.choose_slug("robot-vacuum-guide", today) == "robot-vacuum-guide"
    assert ai_draft.choose_slug("Robot Vacuum!", today) == "guide-draft-2026-11-22"
    assert ai_draft.choose_slug("mobile-battery-guide", today) == "mobile-battery-guide-2026-11-22"
    existing = ai_draft.load_existing_articles()
    assert "/articles/mobile-battery-guide/" in ai_draft.format_existing(existing)

    assert not ai_draft.already_drafted(today)
    text = ai_draft.build_draft_file({"title": "ロボット掃除機の選び方", "body": "本文"}, today, [])
    assert "ai-draft: 2026-11-22" in text and "【要確認" in text
    (tmp_path / "robot-vacuum-guide.md").write_text(text, encoding="utf-8")
    assert ai_draft.already_drafted(today)


def test_sanitize_keeps_links_to_existing_articles():
    body = "[選び方](/articles/mobile-battery-guide/) と [架空](/articles/nothing/)"
    cleaned, removed = ai_draft.sanitize_body(body, ALLOWED | {"/articles/mobile-battery-guide/"})
    assert "(/articles/mobile-battery-guide/)" in cleaned
    assert "/articles/nothing/" not in cleaned and removed == ["/articles/nothing/"]


def test_unfinished_markers_are_reported():
    import fetch_and_build as fb
    from types import SimpleNamespace as NS
    arts = [NS(title="A", slug="a", body_html="<p>【要確認：数字】</p>"), NS(title="B", slug="b", body_html="<p>ok</p>")]
    assert len(fb.find_unfinished_articles(arts)) == 1
