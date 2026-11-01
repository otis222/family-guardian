"""個資遮罩（SPEC 第 7 節；測試清單第 3 項：電話、身分證字號、帳號）。"""

import pytest

from guardian.redact import redact, redact_with_counts


@pytest.mark.parametrize("text, placeholder, secret", [
    ("打給我 0912-345-678", "＜電話＞", "345-678"),
    ("打給我 0912345678", "＜電話＞", "0912345678"),
    ("國際碼 +886 912 345 678", "＜電話＞", "912 345 678"),
    ("公司電話 (02)2345-6789", "＜電話＞", "2345-6789"),
    ("公司電話 02-2345-6789", "＜電話＞", "2345-6789"),
    ("全形 ０９１２３４５６７８", "＜電話＞", "０９１２"),
    ("身分證 A123456789", "＜身分證＞", "A123456789"),
    ("身分證 a223456789", "＜身分證＞", "a223456789"),
    ("居留證 A800000014", "＜身分證＞", "A800000014"),
    ("帳號 822-12345678901234", "＜帳號＞", "12345678901234"),
    ("帳號 00123456789012", "＜帳號＞", "00123456789012"),
    ("卡號 1234 5678 9012 3456", "＜帳號＞", "9012 3456"),
    ("包裹寄到台北市中正區重慶南路一段122號3樓", "＜地址＞", "122號"),
    ("住在中山路100巷5弄3號", "＜地址＞", "5弄3號"),
])
def test_personal_data_is_masked(text, placeholder, secret):
    masked = redact(text)
    assert placeholder in masked
    assert secret not in masked


@pytest.mark.parametrize("text", [
    "晚上聚餐我出 3000 元",
    "2026-11-08 18:30 見",
    "訂單 12345678 已出貨",
    "在第 3 號出口等",
    "早安！今天天氣很好",
])
def test_ordinary_numbers_are_kept(text):
    assert redact(text) == text


def test_urls_are_kept_but_query_is_removed():
    masked = redact("網址 https://x.example/form?phone=0912345678 電話 0912345678")
    assert masked == "網址 https://x.example/form 電話 ＜電話＞"


def test_counts_by_type():
    _, counts = redact_with_counts("0912345678、A123456789、台北市中山區民生東路100號")
    assert counts == {"phone": 1, "national_id": 1, "address": 1}
