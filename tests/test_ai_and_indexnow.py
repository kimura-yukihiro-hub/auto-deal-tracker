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


def test_sanitize_fixes_fullwidth_link_brackets():
    cleaned, removed = ai_draft.sanitize_body("［選び方］(/articles/mobile-battery-guide/)の記事", {"/articles/mobile-battery-guide/"})
    assert cleaned == "[選び方](/articles/mobile-battery-guide/)の記事" and removed == []


def test_choose_format_rotates_and_avoids_recent():
    assert ai_draft.choose_format([])["name"] == "選び方"
    used = [{"format": "選び方"}, {"format": "2製品の比較"}]
    picked = ai_draft.choose_format(used)["name"]
    assert picked not in ("選び方", "2製品の比較")
    # 全部使い終わったら、一番昔に使った型に戻る
    all_used = [{"format": f["name"]} for f in ai_draft.FORMATS]
    assert ai_draft.choose_format(all_used)["name"] == ai_draft.FORMATS[0]["name"]


def test_prompts_render_for_every_format():
    for fmt in ai_draft.FORMATS:
        text = ai_draft.USER_PROMPT_TEMPLATE.format(
            start="2026-11-16", end="2026-11-22", items="[1] x", existing="（なし）", recent="（なし）",
            format_name=fmt["name"], format_topic=fmt["topic"], format_structure=fmt["structure"], format_title=fmt["title"])
        assert "## 結論" in text and ai_draft.PRODUCT_MEMO in text and ai_draft.EXPERIENCE_MEMO in text


def _good_body():
    return (
        "そのイヤホン、通勤ではうるさすぎるかもしれません。\n" + ai_draft.EXPERIENCE_MEMO + "\n\n"
        "## 結論：使い方別のおすすめ早見表\n\n| こんな人に | おすすめ | 価格の目安 |\n|---|---|---|\n"
        f"| 通勤 | {ai_draft.PRODUCT_MEMO} | {ai_draft.PRODUCT_MEMO} |\n\n## まとめ\n\n- a\n\n買う前に、価格を確かめてください。"
    )


def test_mechanical_checks_pass_and_fail():
    assert ai_draft.mechanical_checks({"title": "イヤホンの選び方｜通勤別のおすすめ", "body": _good_body()}) == []
    bad = {"title": "あ" * 40, "body": "今回はイヤホンについて解説します。\n## 選び方\n本文\nいかがでしたか？"}
    problems = " ".join(ai_draft.mechanical_checks(bad))
    for word in ("タイトル", "結論", "商品", "体験", "最初の1文", "最後の1文"):
        assert word in problems


def test_create_draft_reviews_revises_and_keeps_best(tmp_path, monkeypatch):
    """1回目は不合格 → 書き直し → 合格した版が下書きになり、型の履歴が残る。"""
    monkeypatch.setattr(ai_draft, "ARTICLES_DIR", tmp_path / "articles")
    monkeypatch.setattr(ai_draft, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(ai_draft, "BASE_DIR", tmp_path)
    monkeypatch.setattr(ai_draft, "GEMINI_API_KEY", "dummy")
    monkeypatch.setenv("FORCE_AI_DRAFT", "1")
    entries = [{"title": f"t{i}", "link": f"https://example.com/{i}", "date": "2026-11-20"} for i in range(3)]
    monkeypatch.setattr(ai_draft, "load_week_entries", lambda today: entries)
    calls = []

    def fake_call(system, prompt, temperature=None, retries=3):
        calls.append(system[:10])
        if system == ai_draft.REVIEW_SYSTEM:
            ok = "1 回目" not in prompt
            return json.dumps({"scores": {"基準": 5 if ok else 2}, "must_fix": [] if ok else ["直す"], "pass": ok})
        title = "1 回目" if "修正指示" not in prompt else "イヤホンの選び方｜通勤別"
        return json.dumps({"title": title, "slug": "earphone-test", "description": "d", "tags": ["a"], "body": _good_body()},
                          ensure_ascii=False)

    monkeypatch.setattr(ai_draft, "call_gemini", fake_call)
    ai_draft.create_draft()
    text = (tmp_path / "articles" / "earphone-test.md").read_text(encoding="utf-8")
    assert "title: イヤホンの選び方｜通勤別" in text and "確認役の判定（型: 選び方）: 合格" in text
    assert len(calls) == 4  # 執筆・確認・書き直し・確認
    history = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert history[-1]["format"] == "選び方" and history[-1]["pass"] is True


def test_mechanical_checks_catch_question_opener_and_noisy_headings():
    body = _good_body().replace("そのイヤホン、通勤ではうるさすぎるかもしれません。",
                                "イヤホン選びで迷ったことはありませんか。")
    body += "\n\n## とにかく安く済ませたい人向け（1万円台〜【要確認：価格帯を確認】）\n本文"
    problems = " ".join(ai_draft.mechanical_checks({"title": "イヤホンの選び方", "body": body}))
    assert "問いかけ" in problems and "見出しを短く" in problems


class _Resp:
    def __init__(self, status, text="", data=None):
        self.status_code, self.text, self._data = status, text, data

    def json(self):
        return self._data


def _fake_post(busy_model, busy_status, busy_text):
    calls = []

    def post(url, **kw):
        calls.append(url)
        if f"/models/{busy_model}:" in url:
            return _Resp(busy_status, busy_text)
        return _Resp(200, data={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    return post, calls


def test_call_gemini_switches_to_fallback_when_overloaded(monkeypatch):
    monkeypatch.setattr(ai_draft.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai_draft, "_model", {"name": "busy-model"})
    post, calls = _fake_post("busy-model", 503, "This model is currently experiencing high demand")
    monkeypatch.setattr(ai_draft.requests, "post", post)
    assert ai_draft.call_gemini("s", "p") == "ok"
    assert len(calls) == 3 and ai_draft.FALLBACK_MODEL in calls[-1]
    assert ai_draft._model["name"] == ai_draft.FALLBACK_MODEL  # 残りの呼び出しも予備で


def test_call_gemini_switches_on_daily_quota(monkeypatch):
    monkeypatch.setattr(ai_draft.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai_draft, "_model", {"name": "busy-model"})
    post, calls = _fake_post("busy-model", 429, "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    monkeypatch.setattr(ai_draft.requests, "post", post)
    assert ai_draft.call_gemini("s", "p") == "ok"
    assert len(calls) == 2


def test_mechanical_checks_flags_many_people_opening():
    body = _good_body().replace("そのイヤホン、通勤ではうるさすぎるかもしれません。", "どちらを買うべきか迷う人は多いです。")
    problems = " ".join(ai_draft.mechanical_checks({"title": "イヤホンの選び方｜通勤別のおすすめ", "body": body}))
    assert "ありきたり" in problems
