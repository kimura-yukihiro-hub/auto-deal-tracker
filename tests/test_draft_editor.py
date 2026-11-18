from datetime import date

import ai_draft
import articles
import draft_editor as de
import rakuten_links as rl

TODAY = date(2026, 11, 18)


def _draft(tmp_path):
    body = (
        "最初の一文です。\n" + ai_draft.EXPERIENCE_MEMO + "\n\n"
        "## 結論：使い方別のおすすめ早見表\n\n"
        "| こんな人に | おすすめ | 価格の目安 |\n| --- | --- | --- |\n"
        f"| 安く | {ai_draft.PRODUCT_MEMO} | {ai_draft.PRODUCT_MEMO} |\n"
        f"| 在宅 | {ai_draft.PRODUCT_MEMO} | {ai_draft.PRODUCT_MEMO} |\n\n"
        "## 選び方1：大きさ\n\n27インチが人気です【要確認：人気の大きさを確認】。\n\n"
        "## 予算別のおすすめ\n\n### 安く\n\n説明。\n\n" + ai_draft.PRODUCT_MEMO + "\n\n"
        "### 在宅\n\n説明。\n\n" + ai_draft.PRODUCT_MEMO + "\n\n## まとめ\n\n- a\n\n確かめてから買いましょう。\n"
    )
    text = ai_draft.build_draft_file({"title": "モニターの選び方", "body": body, "tags": "a"}, date(2026, 11, 17), [])
    path = tmp_path / "monitor-guide.md"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_network(monkeypatch):
    monkeypatch.setattr(de, "resolve", lambda u: "https://item.rakuten.co.jp/shop/" + u.rstrip("/").rsplit("/", 1)[-1] + "/")
    monkeypatch.setattr(de, "fetch_item", lambda u: {"url": u, "name": "自動の商品名", "price": 19800})


def test_normalize_and_short_name():
    assert rl.normalize("https://item.rakuten.co.jp/shop/item/?scid=af_x&s=1") == "https://item.rakuten.co.jp/shop/item/"
    inner = "https%3A%2F%2Fitem.rakuten.co.jp%2Fshop%2Fitem%2F"
    assert rl.normalize(f"https://hb.afl.rakuten.co.jp/hgc/x/?pc={inner}") == "https://item.rakuten.co.jp/shop/item/"
    assert rl.normalize("https://example.com/item/") is None
    assert rl.is_short_link("https://a.r10.to/hAbCdE") and not rl.is_short_link("https://item.rakuten.co.jp/a/b/")
    name = rl.short_name("【楽天1位】【P10倍】 ワイヤレス充電器 3in1 折りたたみ MagSafe 対応 iPhone AirPods Apple Watch 15W 急速：ショップ楽天市場店")
    assert not name.startswith("【") and "楽天市場店" not in name and len(name) <= rl.MAX_NAME_LENGTH + 1


def test_short_link_in_article_becomes_affiliate(monkeypatch):
    monkeypatch.setattr(articles, "expand_for_affiliate", lambda u: "https://item.rakuten.co.jp/shop/item/" if "r10.to" in u else u)
    html, has_affiliate = articles.render_markdown("[買う](https://a.r10.to/hAbCdE)")
    assert has_affiliate and "af.moshimo.com" in html and "r10.to" not in html


def test_parse_commands():
    cmds = de.parse_commands("1 https://a.r10.to/x 短い名前\n２\n価格 2 12,800円\n体験：良かった\n要確認OK\n公開\nただのメモ")
    assert ("product", 1, "https://a.r10.to/x", "短い名前") in cmds
    assert ("price", 2, 12800) in cmds and ("experience", "良かった") in cmds
    assert ("checks_ok",) in cmds and ("publish",) in cmds and len(cmds) == 5


def test_slots_pair_table_and_sections_and_numbers_stay(tmp_path, monkeypatch):
    _fake_network(monkeypatch)
    path = _draft(tmp_path)
    reply, changed, published = de.run("2 https://a.r10.to/bbb", path, TODAY)
    assert changed and not published and "2 番（在宅）に入れました" in reply
    reply, _, _ = de.run("1 https://a.r10.to/aaa 安い名前\n価格 1 9800", path, TODAY)
    text = path.read_text(encoding="utf-8")
    assert "| 安く | [安い名前](https://item.rakuten.co.jp/shop/aaa/) | 約9,800円（11月18日時点）" in text
    assert "| 在宅 | [自動の商品名](https://item.rakuten.co.jp/shop/bbb/) | 約19,800円（11月18日時点）" in text
    assert "おすすめ：[安い名前](https://item.rakuten.co.jp/shop/aaa/)" in text  # 後半の欄にも入る
    assert ai_draft.PRODUCT_MEMO not in text and "✅ **1**" in reply and "✅ **2**" in reply


def test_publish_requires_products_and_checks(tmp_path, monkeypatch):
    _fake_network(monkeypatch)
    path = _draft(tmp_path)
    reply, _, published = de.run("公開", path, TODAY)
    assert not published and "商品の欄がまだ残っています" in reply and "【要確認：…】が残っています" in reply
    de.run("1 https://a.r10.to/a\n2 https://a.r10.to/b\n体験 2年使っています", path, TODAY)
    reply, _, published = de.run("要確認OK\n公開", path, TODAY)
    assert published and "/articles/monitor-guide/" in reply
    text = path.read_text(encoding="utf-8")
    assert "draft:" not in text and "date: 2026-11-18" in text
    for leftover in ("<!--", "【要確認", "【編集者メモ", "slot:"):
        assert leftover not in text
    assert "2年使っています" in text
    # 公開後のファイルは、サイトの記事として読み込める
    article = articles.load_article(path, "auto-deal-tracker.pages.dev", TODAY)
    assert article is not None and article.title == "モニターの選び方"


def test_unknown_comment_is_ignored(tmp_path):
    path = _draft(tmp_path)
    before = path.read_text(encoding="utf-8")
    assert de.run("ありがとう、あとで見ます", path, TODAY) == ("", False, False)
    assert path.read_text(encoding="utf-8") == before


def test_parse_edit_commands_keep_fullwidth_spaces():
    cmds = de.parse_commands("直す 2年　使って → 3年使って\n直す：60Hz->75Hz\n削除 一般的な、\nタイトル　新しい題")
    assert ("replace", "2年　使って", "3年使って") in cmds  # 全角スペースはそのまま
    assert ("replace", "60Hz", "75Hz") in cmds
    assert ("replace", "一般的な、", "") in cmds and ("title", "新しい題") in cmds


def test_replace_delete_and_title(tmp_path, monkeypatch):
    _fake_network(monkeypatch)
    path = _draft(tmp_path)
    de.run("体験 2年使っています", path, TODAY)
    reply, changed, _ = de.run(
        "直す 2年使っています → 3年、毎日使っています\n削除 27インチが人気です\nタイトル 在宅ワークが変わるモニター選び", path, TODAY)
    text = path.read_text(encoding="utf-8")
    assert changed and "3年、毎日使っています" in text and "2年使っています" not in text
    assert "27インチが人気です" not in text and "【要確認：人気の大きさを確認】" in text  # 消したのは指定した文字だけ
    assert "title: 在宅ワークが変わるモニター選び" in text and "**タイトル**: 在宅ワークが変わるモニター選び" in reply


def test_replace_refuses_missing_or_ambiguous_and_ignores_notes(tmp_path):
    path = _draft(tmp_path)
    before = path.read_text(encoding="utf-8")
    reply, changed, _ = de.run("直す ないない → あるある\n直す 説明。 → 別の説明。", path, TODAY)
    assert not changed and path.read_text(encoding="utf-8") == before
    assert "見つかりませんでした" in reply and "2 か所あるので" in reply
    # 公開前メモ（<!-- … -->）の中の文字は直さない
    reply, changed, _ = de.run("削除 この下書きは AI（Gemini）が自動で作成しました", path, TODAY)
    assert not changed and "見つかりませんでした" in reply


CANDS = [
    {"url": "https://item.rakuten.co.jp/s/a/", "name": "候補A", "price": 9800, "review_average": 4.5, "review_count": 120, "shop": "s"},
    {"url": "https://item.rakuten.co.jp/s/b/", "name": "候補B", "price": 12800, "review_average": 4.3, "review_count": 80, "shop": "s"},
]


def test_choose_candidate_and_search(tmp_path, monkeypatch):
    monkeypatch.setattr(de, "CANDIDATES_DIR", tmp_path / "cands")
    path = _draft(tmp_path)
    de.save_candidates(path.stem, {"1": CANDS})
    reply, changed, _ = de.run("一覧", path, TODAY)
    assert "**A**: [候補A](https://item.rakuten.co.jp/s/a/) 9,800円 ★4.5（120件）" in reply  # 空いている欄に候補が出る
    reply, changed, _ = de.run("１ ｂ\n1 C", path, TODAY)  # 全角でも読める。C はない
    text = path.read_text(encoding="utf-8")
    assert changed and "[候補B](https://item.rakuten.co.jp/s/b/) | 約12,800円（11月18日時点）" in text
    assert "候補 C はありません" in reply
    # 探し直し: 価格帯つき
    calls = []
    monkeypatch.setattr(de, "search_items", lambda kw, lo, hi: calls.append((kw, lo, hi)) or CANDS[:1])
    reply, changed, _ = de.run("候補 2 PCモニター 27インチ 20000-40000", path, TODAY)
    assert calls == [("PCモニター 27インチ", 20000, 40000)] and changed
    assert de.load_candidates(path.stem)["2"] == CANDS[:1] and "`2 A` のように送ると入ります" in reply


def test_search_error_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(de, "CANDIDATES_DIR", tmp_path / "cands")
    path = _draft(tmp_path)

    def boom(*a):
        raise de.SearchError("楽天のアプリID（RAKUTEN_APP_ID）が設定されていません")

    monkeypatch.setattr(de, "search_items", boom)
    reply, changed, _ = de.run("候補 1 モニター", path, TODAY)
    assert not changed and "候補を探せませんでした" in reply


def test_make_candidates_from_ai_searches(tmp_path, monkeypatch):
    import rakuten_links

    monkeypatch.setattr(de, "CANDIDATES_DIR", tmp_path / "cands")
    monkeypatch.setattr(ai_draft.time, "sleep", lambda s: None)
    calls = []

    def fake_search(kw, lo=None, hi=None):
        calls.append((kw, lo, hi))
        return [] if lo else CANDS  # 価格帯つきで 0 件なら、価格帯なしで探し直す

    monkeypatch.setattr(rakuten_links, "search_items", fake_search)
    path = _draft(tmp_path)
    ai_draft.make_candidates(path, [{"keyword": "モニター 安い", "min_price": 5000, "max_price": 15000},
                                    {"keyword": "モニター 在宅", "min_price": None, "max_price": None}])
    saved = de.load_candidates(path.stem)
    assert set(saved) == {"1", "2"} and calls[0] == ("モニター 安い", 5000, 15000) and calls[1] == ("モニター 安い", None, None)


def test_publish_reply_has_x_posts_with_url(tmp_path, monkeypatch):
    _fake_network(monkeypatch)
    monkeypatch.setattr(de, "X_POSTS_DIR", tmp_path / "x")
    monkeypatch.setattr(ai_draft, "X_POSTS_DIR", tmp_path / "x")
    path = _draft(tmp_path)
    ai_draft.save_x_posts(path.stem, ["モニター選び、画面の大きさだけで決めていませんか。 #在宅ワーク https://bad.example.com", "あ" * 200])
    de.run("1 https://a.r10.to/a\n2 https://a.r10.to/b", path, TODAY)
    reply, _, published = de.run("要確認OK\n公開", path, TODAY)
    assert published and "📣 X の告知文" in reply
    assert "#在宅ワーク\nhttps://auto-deal-tracker.pages.dev/articles/monitor-guide/" in reply
    assert "bad.example.com" not in reply and "あ" * 200 not in reply  # 余計な URL と長すぎる案は除く


def test_publish_reply_x_posts_fallback(tmp_path, monkeypatch):
    _fake_network(monkeypatch)
    monkeypatch.setattr(de, "X_POSTS_DIR", tmp_path / "none")
    path = _draft(tmp_path)
    de.run("1 https://a.r10.to/a\n2 https://a.r10.to/b", path, TODAY)
    reply, _, published = de.run("要確認OK\n公開", path, TODAY)
    assert published and "「モニターの選び方」\nhttps://auto-deal-tracker.pages.dev/articles/monitor-guide/" in reply


def test_parse_replace_block():
    """編集ページが送る置き換えブロック（矢印・複数行・削除）と、ふつうの指示を一緒に読める。"""
    comment = (
        "【置き換え】\r\nA → B の順に選ぶ\r\n2行目\r\n【↓】\r\nC → D の順に選ぶ\r\n【ここまで】\r\n"
        "【置き換え】\n消す文\n【↓】\n【ここまで】\n"
        "タイトル 新しい題\n公開"
    )
    assert de.parse_commands(comment) == [
        ("replace", "A → B の順に選ぶ\n2行目", "C → D の順に選ぶ"),
        ("replace", "消す文", ""),
        ("title", "新しい題"),
        ("publish",),
    ]


def test_publish_ignores_check_marks_in_notes():
    """本文の【要確認】を先に直したあとでも、公開前メモの中の説明文（【要確認：…】）に引っかからず公開できる。"""
    from datetime import date
    text = "---\ntitle: t\ndate: 2026-11-01\ndraft: true\n---\n\n<!--\n  1. 【要確認：…】の部分を調べて消す\n-->\n\n本文です。\n"
    out, problems = de.publish_text(text, date(2026, 11, 18))
    assert problems == []
    assert "draft:" not in out and "要確認" not in out and "date: 2026-11-18" in out
    out, problems = de.publish_text(text.replace("本文です。", "本文です【要確認：数字】。"), date(2026, 11, 18))
    assert out is None and problems
