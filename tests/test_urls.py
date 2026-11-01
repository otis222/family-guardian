"""網址抽取與正規化（SPEC 3-1，決策 D6）。"""

import pytest

from guardian.urls import domain_suffixes, extract_urls, url_host


@pytest.mark.parametrize("text, expected", [
    ("看這個 https://stock-teacher.example/join", ["https://stock-teacher.example/join"]),
    ("請點擊abc.com/xyz?id=0912345678領取", ["http://abc.com/xyz"]),  # 緊貼中文、拿掉查詢參數
    ("ｗｗｗ．ＢＡＤ．ｃｏｍ／ｌｏｇｉｎ 快點", ["http://www.bad.com/login"]),  # 全形寫法
    ("官網 https://WWW.Example.COM/Path/?utm=1#top。", ["https://www.example.com/Path"]),
    ("加我 https://line.me/ti/p/~abc123，或 lin.ee/xYz", ["https://line.me/ti/p/~abc123", "http://lin.ee/xYz"]),
    ("同一個網址 https://a.example 再一次 https://a.example/", ["https://a.example"]),
    ("IP 網址 http://203.0.113.5/a", ["http://203.0.113.5/a"]),
])
def test_extract_urls(text, expected):
    assert extract_urls(text) == expected


@pytest.mark.parametrize("text", [
    "寄信到 someone@gmail.com 就好",
    "報告在 report.pdf，版本 v1.2.3",
    "比數 1.5 比 2.0",
    "早安！今天天氣很好",
])
def test_extract_urls_ignores_non_urls(text):
    assert extract_urls(text) == []


def test_url_host_normalizes():
    assert url_host("https://Login.Bad.EXAMPLE:8443/x") == "login.bad.example"
    assert url_host("bad.example") == "bad.example"
    assert url_host("https://例子.測試").startswith("xn--")  # 中文網域轉 punycode
    assert url_host("不是網址") is None
    assert url_host("http://1.2.3") is None


def test_domain_suffixes_strip_www_and_stop_before_tld():
    assert domain_suffixes("www.login.bad.example") == ["login.bad.example", "bad.example"]
    assert domain_suffixes("bad.example") == ["bad.example"]
