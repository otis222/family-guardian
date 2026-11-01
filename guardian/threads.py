"""Thread 訂閱（SPEC 3-5，決策 D10、D12）：開啟、查找、過期。只有記憶體，不落地。

有網址的訊息用網域當 key，否則用發送者：範圍越窄，送進模型的訊息越少。
"""

from __future__ import annotations

from datetime import datetime

from .models import Thread, Trigger
from .urls import domain_suffixes, url_host


def _domain(url: str) -> str | None:
    host = url_host(url)
    return domain_suffixes(host)[0] if host else None


class ThreadStore:
    def __init__(self, ttl_hours: int = 24) -> None:
        self.ttl_hours = ttl_hours
        self._threads: dict[tuple[str, str], Thread] = {}

    def __len__(self) -> int:
        return len(self._threads)

    def open(self, trigger: Trigger) -> Thread | None:
        """替這則訊息開 Thread；已經有就沿用。不在群組裡的訊息不開。"""
        if not trigger.group_id:
            return None
        domain = next(filter(None, map(_domain, trigger.urls)), None)
        value, key_type = (domain, "domain") if domain else (trigger.sender_hash, "sender")
        key = (trigger.group_id, value)
        thread = self._threads.get(key)
        if thread is None:
            thread = Thread.open(group_id=trigger.group_id, value=value, key_type=key_type,
                                 now=trigger.time, ttl_hours=self.ttl_hours)
            self._threads[key] = thread
        return thread

    def find(self, trigger: Trigger) -> Thread | None:
        """同網域優先，其次同發送者。呼叫前先 expire()。"""
        if not trigger.group_id:
            return None
        for url in trigger.urls:
            thread = self._threads.get((trigger.group_id, _domain(url) or ""))
            if thread:
                return thread
        return self._threads.get((trigger.group_id, trigger.sender_hash))

    def expire(self, now: datetime) -> list[Thread]:
        expired = [thread for thread in self._threads.values() if thread.is_expired(now)]
        for thread in expired:
            del self._threads[thread.key]
        return expired

    def close(self, key: tuple[str, str]) -> Thread | None:
        """提前結束（Watchdog 判斷鬼打牆時用）。"""
        return self._threads.pop(key, None)
