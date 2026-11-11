import pytest

from affiliate import build_moshimo_rakuten_link, is_rakuten_ichiba_url, moshimo_rakuten_impression_url
from digest import is_sale_title


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://item.rakuten.co.jp/shop/item-1/", True),
        ("https://search.rakuten.co.jp/search/mall/Anker/", True),
        ("https://www.rakuten.co.jp/", True),
        ("https://hb.afl.rakuten.co.jp/hgc/abc/", False),  # 既存の楽天アフィリエイトリンクは二重変換しない
        ("https://www.itmedia.co.jp/news/a.html", False),
        ("https://evil.example.com/?r=rakuten.co.jp", False),
        ("https://rakuten.co.jp.evil.example.com/", False),
        ("", False),
    ],
)
def test_is_rakuten_ichiba_url(url, expected):
    assert is_rakuten_ichiba_url(url) is expected


def test_moshimo_link_format():
    link = build_moshimo_rakuten_link("https://item.rakuten.co.jp/shop/item-1/?a=1&b=日本")
    assert link.startswith("https://af.moshimo.com/af/c/click?a_id=5891942&p_id=54&pc_id=54&pl_id=616&url=")
    assert link.endswith("https%3A%2F%2Fitem.rakuten.co.jp%2Fshop%2Fitem-1%2F%3Fa%3D1%26b%3D%E6%97%A5%E6%9C%AC")
    assert moshimo_rakuten_impression_url() == (
        "https://i.moshimo.com/af/i/impression?a_id=5891942&p_id=54&pc_id=54&pl_id=616"
    )


@pytest.mark.parametrize(
    "title, expected",
    [
        ("【本日みつけたお買い得品】Kindleが約2万円オフ", True),
        ("Anker充電器がセール中で33％オフ", True),
        ("マウスが20%OFFで販売", True),
        ("EdifierのPCスピーカーがスマイルSaleでオトク", True),
        ("Amazon BIG SALE 開催", True),
        ("Seagate 8TB HDDが一時3万円を切る特売", True),
        ("SSDが値下がり", True),
        ("約700円引き！", True),
        ("Googleのブラックフライデーは20日から", True),
        ("新製品を発表", False),
        ("円安で値上げ", False),
        ("単品5010円から登場", False),
        ("3日間限定で全文無料公開", False),
        ("Salesforceが新AIを発表", False),
        ("Sales部門を再編", False),
    ],
)
def test_is_sale_title(title, expected):
    assert is_sale_title(title) is expected
