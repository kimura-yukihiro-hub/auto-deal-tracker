import re
from datetime import date

from articles import load_articles, render_markdown

HOST = "auto-deal-tracker.pages.dev"
TODAY = date(2026, 11, 15)


def write(dir_, name, front, body="本文です。"):
    (dir_ / name).write_text(f"---\n{front}\n---\n{body}\n", encoding="utf-8")


def test_only_publishable_articles_are_loaded(tmp_path):
    write(tmp_path, "good-post.md", "title: 公開記事\ndate: 2026-11-10\ntags: a, b")
    write(tmp_path, "draft-post.md", "title: 下書き\ndate: 2026-11-10\ndraft: true")
    write(tmp_path, "future-post.md", "title: 予約\ndate: 2026-12-01")
    write(tmp_path, "no-title.md", "date: 2026-11-10")
    write(tmp_path, "日本語.md", "title: 名前が不正\ndate: 2026-11-10")
    write(tmp_path, "_template.md", "title: テンプレート\ndate: 2026-11-10")
    articles = load_articles(tmp_path, HOST, today=TODAY)
    assert [a.slug for a in articles] == ["good-post"]
    assert articles[0].tags == ["a", "b"]
    assert articles[0].path == "articles/good-post/"


def test_articles_sorted_newest_first_and_updated(tmp_path):
    write(tmp_path, "older.md", "title: 古い\ndate: 2026-11-01\nupdated: 2026-11-09")
    write(tmp_path, "newer.md", "title: 新しい\ndate: 2026-11-10")
    articles = load_articles(tmp_path, HOST, today=TODAY)
    assert [a.slug for a in articles] == ["newer", "older"]
    assert articles[1].last_modified == date(2026, 11, 9)


def test_crlf_front_matter(tmp_path):
    (tmp_path / "crlf-post.md").write_bytes("---\r\ntitle: 改行CRLF\r\ndate: 2026-11-10\r\n---\r\n本文\r\n".encode())
    articles = load_articles(tmp_path, HOST, today=TODAY)
    assert articles[0].title == "改行CRLF"


def test_rakuten_link_becomes_affiliate_with_pixel():
    body, has_affiliate = render_markdown("前[商品](https://item.rakuten.co.jp/shop/item-1/)後ろの文章。", HOST)
    assert has_affiliate
    assert "af.moshimo.com/af/c/click?a_id=5891942" in body
    assert 'rel="nofollow sponsored noopener"' in body
    assert 'referrerpolicy="no-referrer-when-downgrade"' in body
    # 計測ピクセルがリンク直後に入り、後ろの文章が消えない
    assert re.search(r"</a><img[^>]*i\.moshimo\.com[^>]*>後ろの文章。", body)


def test_external_and_internal_links():
    body, has_affiliate = render_markdown(
        f"[外部](https://www.itmedia.co.jp/) [内部](https://{HOST}/articles/) [相対](/articles/)", HOST
    )
    assert not has_affiliate
    assert re.search(r'href="https://www\.itmedia\.co\.jp/"[^>]*target="_blank"|target="_blank"[^>]*href="https://www\.itmedia', body)
    assert f'href="https://{HOST}/articles/">' in body
    assert 'href="/articles/">' in body
