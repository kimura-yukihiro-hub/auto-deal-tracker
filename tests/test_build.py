import json
import re
import xml.dom.minidom
from datetime import date

import fetch_and_build as fb
from articles import Article


def test_render_link_rakuten_and_plain():
    rakuten = fb.render_link("https://item.rakuten.co.jp/a/b/", "見る")
    assert "af.moshimo.com" in rakuten and "i.moshimo.com" in rakuten
    plain = fb.render_link("https://www.itmedia.co.jp/x.html", "見る")
    assert "moshimo" not in plain and 'href="https://www.itmedia.co.jp/x.html"' in plain


def test_featured_json_priority_and_fallback(tmp_path, monkeypatch, make_item):
    items = [
        make_item("新製品発表", "https://ex.com/1", hour=12),
        make_item("マウスが20%OFF", "https://ex.com/2", hour=11),
        make_item("テックの話題", "https://ex.com/3", category="テック", hour=10),
        make_item("HDDが特売", "https://ex.com/4", hour=9),
    ]
    featured = tmp_path / "featured.json"
    monkeypatch.setattr(fb, "FEATURED_PATH", featured)

    featured.write_text(json.dumps({"items": [
        {"title": "期限切れ", "url": "https://item.rakuten.co.jp/a/b/", "expires": "2000-01-01"},
        {"title": "有効", "url": "https://item.rakuten.co.jp/a/c/", "price": 3990},
        {"title": "URLなし"},
    ]}), encoding="utf-8")
    picks = fb.select_picks(items)
    assert [p.title for p in picks] == ["有効"] and picks[0].price == "¥3,990"

    for content in ['{"items": []}', '{"items": [ {"title": ', None]:
        if content is None:
            featured.unlink()
        else:
            featured.write_text(content, encoding="utf-8")
        picks = fb.select_picks(items)
        # セール記事を優先し、足りない分をガジェット記事で補う
        assert [p.title for p in picks] == ["マウスが20%OFF", "HDDが特売", "新製品発表"]
        assert not any(p.manual for p in picks)


def test_top_page_structure(make_item):
    items = [make_item(f"ニュース{i}", f"https://ex.com/{i}", category="テック" if i % 2 else "ガジェット", hour=i % 23)
             for i in range(25)]
    items.append(make_item("<script>alert(1)</script>がセール", "https://ex.com/x"))
    html = fb.render_page(items, [], [], [])
    # 絞り込みボタンと件数、全カードのカテゴリにボタンがある
    filters = set(re.findall(r'data-filter="([^"]+)"', html)) - {"all"}
    cats = set(re.findall(r'<article class="card" data-category="([^"]+)"', html))
    assert cats <= filters and "セール" in filters
    assert '<span class="count">26</span>' in html
    # 「もっと見る」ボタン（JS 無効時は hidden のまま全件表示）とセール速報欄
    assert 'id="sale"' in html
    assert re.search(r'<button type="button" id="more-news" class="more-btn" hidden>', html)
    # JS が動かない環境・検索エンジンには全件が見える（HTML の時点ではカードを隠さない）
    assert html.count('<article class="card"') == 26
    assert not re.search(r'<article class="card"[^>]*hidden', html)
    # タイトルはリンクになり、HTML はエスケープされる
    assert '<h3><a href="https://ex.com/0" target="_blank" rel="noopener">ニュース0</a></h3>' in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_seo_files_and_404(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "OUTPUT_PATH", tmp_path / "index.html")
    article = Article(slug="hello", title="こんにちは", date=date(2026, 11, 10), description="d", body_html="<p>x</p>")
    fb.write_seo_files([article])
    sitemap = (tmp_path / "sitemap.xml").read_text(encoding="utf-8")
    xml.dom.minidom.parseString(sitemap)  # 正しい XML か
    assert f"<loc>{fb.SITE_URL}articles/hello/</loc>" in sitemap
    assert "<lastmod>2026-11-10</lastmod>" in sitemap
    assert f"Sitemap: {fb.SITE_URL}sitemap.xml" in (tmp_path / "robots.txt").read_text(encoding="utf-8")
    assert (tmp_path / f"{fb.INDEXNOW_KEY}.txt").read_text(encoding="utf-8") == fb.INDEXNOW_KEY
    assert fb.BING_SITE_VERIFICATION in (tmp_path / "BingSiteAuth.xml").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex">' in fb.render_not_found_page()


def test_article_page_pr_note(make_item):
    plain = Article(slug="a", title="普通", date=date(2026, 11, 10), description="d", body_html="<p>x</p>")
    ad = Article(slug="b", title="広告", date=date(2026, 11, 10), description="d", body_html="<p>x</p>", has_affiliate=True)
    assert 'class="pr-note"' not in fb.render_article_page(plain, [plain, ad])
    page = fb.render_article_page(ad, [plain, ad])
    assert 'class="pr-note"' in page and '"@type": "BlogPosting"' in page
    assert f'<link rel="canonical" href="{fb.SITE_URL}articles/b/">' in page


def test_footer_contact(monkeypatch):
    monkeypatch.setattr(fb, "CONTACT_FORM_URL", "")
    monkeypatch.setattr(fb, "CONTACT_EMAIL", "")
    assert "準備中" in fb.render_footer()
    monkeypatch.setattr(fb, "CONTACT_FORM_URL", "https://forms.gle/example")
    footer = fb.render_footer()
    assert '<a class="contact-btn" href="https://forms.gle/example" target="_blank" rel="noopener">' in footer
    assert "準備中" not in footer
