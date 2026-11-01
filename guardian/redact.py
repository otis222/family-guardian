"""個資遮罩（SPEC 第 7 節）：送進模型之前，遮掉電話、身分證字號、帳號和地址。

網址不遮罩，只換成正規化後的形式（拿掉查詢參數和錨點，決策 D6），避免遮罩把網址改壞。
"""

from __future__ import annotations

import re
from collections import Counter

from .urls import find_urls, normalize_url, normalize_width

PLACEHOLDERS = {
    "national_id": "＜身分證＞",
    "phone": "＜電話＞",
    "account": "＜帳號＞",
    "address": "＜地址＞",
}

_CJK = "一-鿿"
_ADDRESS = (
    rf"(?:[{_CJK}]{{2}}[縣市])?(?:[{_CJK}]{{1,3}}[區鄉鎮市])?[{_CJK}0-9]{{1,6}}(?:路|街|大道)\s?"
    r"(?:[一二三四五六七八九十0-9]+段)?\s?(?:[0-9]+巷)?\s?(?:[0-9]+弄)?\s?[0-9]+(?:之[0-9]+)?\s?號"
    r"(?:\s?[0-9]+樓)?(?:之[0-9]+)?"
)

# 依序套用：先身分證和電話，再把剩下的長串數字當成帳號
_PATTERNS = (
    # 身分證：英文字母＋1/2（國民）或 8/9、A–D（居留證）＋8 碼數字
    ("national_id", re.compile(r"(?<![A-Za-z0-9])[A-Za-z](?:[1289]|[A-Da-d])[0-9]{8}(?![0-9])")),
    ("phone", re.compile(
        r"(?<![0-9])(?:\+?886[-\s]?|0)9[0-9]{2}[-\s]?[0-9]{3}[-\s]?[0-9]{3}(?![0-9])"  # 手機
        r"|\(0[2-8][0-9]?\)\s?[0-9]{3,4}[-\s]?[0-9]{4}(?![0-9])"  # (02)2345-6789
        r"|(?<![0-9])(?:\+?886[-\s]?|0)[2-8][0-9]?-[0-9]{3,4}-?[0-9]{4}(?![0-9])"  # 02-2345-6789
        r"|(?<![0-9])0[2-8][0-9]{7,8}(?![0-9])"  # 0223456789
    )),
    ("account", re.compile(
        r"(?<![0-9])[0-9]{4}(?:[\s-][0-9]{4}){3}(?![0-9])"  # 卡號 1234 5678 9012 3456
        r"|(?<![0-9])[0-9](?:-?[0-9]){9,18}(?![0-9])"  # 10 到 19 位數字（可含 -）
    )),
    ("address", re.compile(_ADDRESS)),
)


def _mask(segment: str, counts: Counter) -> str:
    for name, pattern in _PATTERNS:
        segment, found = pattern.subn(PLACEHOLDERS[name], segment)
        if found:
            counts[name] += found
    return segment


def redact_with_counts(text: str) -> tuple[str, Counter]:
    """回傳遮罩後的文字和各類個資的數量。"""
    text = normalize_width(text)
    counts: Counter = Counter()
    pieces: list[str] = []
    last = 0
    for start, end in find_urls(text):
        pieces.append(_mask(text[last:start], counts))
        url = normalize_url(text[start:end])
        pieces.append(url if url else _mask(text[start:end], counts))
        last = end
    pieces.append(_mask(text[last:], counts))
    return "".join(pieces), counts


def redact(text: str) -> str:
    return redact_with_counts(text)[0]
