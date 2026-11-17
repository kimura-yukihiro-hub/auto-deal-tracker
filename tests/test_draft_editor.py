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
