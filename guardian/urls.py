"""網址抽取與正規化（SPEC 3-1、3-2，決策 D6）。

- 抽得到：有協定的網址、www. 開頭的網址、常見頂級網域的裸網域（例如 abc.com/xyz），也包含全形字寫的網址。
- 正規化：補上協定、網域轉小寫（中文網域轉成 punycode），拿掉帳密、連接埠、查詢參數和錨點。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# 全形英數和幾個常被拿來躲過濾的符號轉成半形（只影響比對用的文字）
_WIDTH_TABLE = {code: code - 0xFEE0 for code in (*range(0xFF10, 0xFF1A), *range(0xFF21, 0xFF3B), *range(0xFF41, 0xFF5B))}
_WIDTH_TABLE.update({ord("＋"): "+", ord("－"): "-", ord("．"): ".", ord("／"): "/", ord("＠"): "@"})

# 裸網域只認這些頂級網域，避免把檔名、版本號當成網址；有協定或 www. 的網址不受限制
_TLDS = (
    "com", "net", "org", "edu", "gov", "tw", "cc", "top", "xyz", "vip", "me", "io", "app", "shop",
    "site", "online", "club", "info", "biz", "co", "hk", "cn", "jp", "asia", "live", "link", "win",
    "bet", "fun", "icu", "store", "tech", "pro", "ly", "ee", "example",  # ee：LINE 短網址 lin.ee
)
_STOP = "\\s<>\"'\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"  # 空白、引號、中文字和全形標點都代表網址結束
_HOST = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:" + "|".join(_TLDS) + r")(?![a-z0-9-])"
URL_PATTERN = re.compile(
    rf"https?://[^{_STOP}]+"
    rf"|www\.[^{_STOP}]+"
    rf"|(?<![a-z0-9@._-]){_HOST}(?::\d{{2,5}})?(?:/[^{_STOP}]*)?",
    re.IGNORECASE,
)
_TRAILING = ".,;:!?)]}>'\""
_HOST_CHARS = re.compile(r"^[a-z0-9.-]+$")


def normalize_width(text: str) -> str:
    """全形英數與 ＋－．／＠ 轉成半形。"""
    return text.translate(_WIDTH_TABLE)


def find_urls(text: str) -> list[tuple[int, int]]:
    """回傳網址在文字裡的位置（不含結尾標點）。text 要先經過 normalize_width。"""
    spans = []
    for match in URL_PATTERN.finditer(text):
        start, end = match.span()
        while end > start and text[end - 1] in _TRAILING:
            end -= 1
        if end > start:
            spans.append((start, end))
    return spans


def clean_host(host: str | None) -> str | None:
    """網域轉小寫、轉 punycode 並檢查格式；不合格回傳 None。"""
    if not host:
        return None
    host = host.strip().rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if "." not in host or not _HOST_CHARS.match(host) or ".." in host or host.startswith((".", "-")):
        return None
    labels = host.split(".")
    if labels[-1].isdigit():  # 數字結尾只接受 IPv4
        if len(labels) != 4 or not all(label.isdigit() and int(label) <= 255 for label in labels):
            return None
    return host


def normalize_url(raw: str) -> str | None:
    """補上協定，只留協定、網域、路徑。"""
    candidate = raw.strip().rstrip(_TRAILING)
    if not candidate:
        return None
    if not re.match(r"(?i)https?://", candidate):
        candidate = "http://" + candidate
    try:
        parts = urlsplit(candidate)
        host = clean_host(parts.hostname)
    except ValueError:
        return None
    if host is None:
        return None
    return f"{parts.scheme.lower()}://{host}{parts.path.rstrip('/')}"


def extract_urls(text: str) -> list[str]:
    """抽出文字裡的網址並正規化，去掉重複、保持順序。"""
    text = normalize_width(text)
    result: list[str] = []
    for start, end in find_urls(text):
        url = normalize_url(text[start:end])
        if url and url not in result:
            result.append(url)
    return result


def strip_urls(text: str, placeholder: str = "＜網址＞") -> str:
    """把文字裡的網址換成佔位字（模型寫的文字要給家人看之前用）。"""
    text = normalize_width(text)
    pieces: list[str] = []
    last = 0
    for start, end in find_urls(text):
        pieces += [text[last:start], placeholder]
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def url_host(value: str) -> str | None:
    """網址或網域 → 正規化的網域。"""
    value = value.strip()
    if not value:
        return None
    if not re.match(r"(?i)https?://", value):
        value = "http://" + value
    try:
        return clean_host(urlsplit(value).hostname)
    except ValueError:
        return None


def domain_suffixes(host: str) -> list[str]:
    """比對名單用：a.b.example → ['a.b.example', 'b.example']。開頭的 www. 先拿掉。"""
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    return [".".join(labels[index:]) for index in range(len(labels) - 1)]
