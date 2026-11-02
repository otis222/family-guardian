"""找出 cloudflared Quick Tunnel 的公開網址（SPEC 第 10 節）。

Quick Tunnel 每次重開網址都會變。cloudflared 在本機的指標服務（預設埠 20241–20245）有 /quicktunnel，回傳目前的網域。
找不到時回傳 None，用具名通道的話改填 .env 的 PUBLIC_BASE_URL。
"""

from __future__ import annotations

import http.client
import json
import urllib.request
from typing import Callable

METRICS_PORTS = range(20241, 20246)
QUICK_TUNNEL_SUFFIX = ".trycloudflare.com"

Fetcher = Callable[[str], bytes]


def _http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310（只連本機）
        return response.read(4096)


def find_quick_tunnel_url(fetch: Fetcher = _http_get) -> str | None:
    for port in METRICS_PORTS:
        try:
            data = json.loads(fetch(f"http://127.0.0.1:{port}/quicktunnel"))
        except (OSError, ValueError, http.client.HTTPException):
            continue  # 這個埠沒有 cloudflared
        hostname = data.get("hostname") if isinstance(data, dict) else None
        if isinstance(hostname, str) and hostname.endswith(QUICK_TUNNEL_SUFFIX):
            return f"https://{hostname}"
    return None
