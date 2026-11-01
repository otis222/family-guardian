"""引擎（SPEC 3-1、3-2）：原始事件 → Trigger → 快速過濾 → 依分數分三段。

目前的範圍（Phase 2）：
- 正規化時抽網址、遮罩個資（guardian/urls.py、guardian/redact.py）。
- 快速過濾用規則、情境加權和 165 名單給分（guardian/rules.py）。
- 觀察區間和候選事件先登記；Thread、提前行動、Router、Worker 在 Phase 3 接上，所以還不會呼叫模型。

原始事件的格式（模擬劇本和 LINE 伺服器都轉成這個格式再交給引擎）：
    {"id", "time", "source", "group_id", "user_id", "kind", "text", "mention", "reply_token"}
user_id 一進引擎就雜湊成 sender_hash，原始值不保存。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from .config import Config
from .llm import LLMClient
from .models import BAND_LABELS, BANDS, KIND_LABELS, EventRecord, RiskSignal, SchemaError, TraceEntry, Trigger
from .redact import redact as default_redact
from .rules import FastFilter
from .urls import extract_urls as default_extract_urls

# 模擬劇本固定用這個鹽，讓 replay 在任何電腦上都得到一樣的 prompt（決策 D4）。
# 真實群組（LINE 伺服器）一律用 .env 裡的 GUARDIAN_HASH_SALT。
DEMO_SALT = "guardian-demo-salt"

RAW_KINDS = ("text", "image", "join", "member_joined")
TEXT_KINDS = ("text", "mention")


def hash_sender(user_id: str, salt: str) -> str:
    """發送者 ID 轉成不可逆的短雜湊。"""
    digest = hashlib.sha256(f"{salt}:{user_id}".encode("utf-8")).hexdigest()
    return "u_" + digest[:12]


def _as_datetime(value: Any, event_id: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise SchemaError(f"事件 {event_id}：time 不是 ISO 8601 格式") from None
    if not isinstance(value, datetime):
        raise SchemaError(f"事件 {event_id}：time 必須是 datetime 或 ISO 8601 字串")
    return value


@dataclass
class RunSummary:
    events: int
    by_kind: dict[str, int]
    by_band: dict[str, int]
    candidates: int
    records: int
    llm: dict[str, Any]


class Engine:
    def __init__(
        self,
        config: Config,
        *,
        salt: str,
        llm_client: LLMClient | None = None,
        redact: Callable[[str], str] | None = None,
        extract_urls: Callable[[str], list[str]] | None = None,
        fast_filter: Callable[[Trigger], RiskSignal] | None = None,
    ) -> None:
        if not isinstance(salt, str) or not salt:
            raise ValueError("Engine 需要雜湊用的鹽：模擬劇本用 DEMO_SALT，LINE 伺服器用 .env 的 GUARDIAN_HASH_SALT")
        self.config = config
        self._salt = salt
        self.llm = llm_client or LLMClient.from_config(config)  # Phase 3 起由 Router、Worker 透過 call_llm 使用
        self.redact = redact or default_redact
        self.extract_urls = extract_urls or default_extract_urls
        self.fast_filter = fast_filter or FastFilter.from_config(config)
        self.trace: list[TraceEntry] = []
        self.records: list[EventRecord] = []
        self.candidate_queue: list[Trigger] = []
        self.counts: Counter[str] = Counter()

    # -------------------------------------------------------------- 正規化

    def normalize(self, raw: Mapping[str, Any]) -> Trigger:
        """原始事件 → Trigger：雜湊發送者、抽網址、遮罩內容。"""
        if not isinstance(raw, Mapping):
            raise SchemaError("事件必須是 dict")
        event_id = raw.get("id")
        kind = raw.get("kind")
        if kind not in RAW_KINDS:
            raise SchemaError(f"事件 {event_id}：kind 必須是 {'、'.join(RAW_KINDS)} 其中之一，收到：{kind!r}")
        mention = raw.get("mention", False)
        if not isinstance(mention, bool):
            raise SchemaError(f"事件 {event_id}：mention 必須是 True 或 False")
        if mention and kind != "text":
            raise SchemaError(f"事件 {event_id}：只有文字訊息可以提及顧家貓")
        user_id = raw.get("user_id")
        if user_id is not None and (not isinstance(user_id, str) or not user_id):
            raise SchemaError(f"事件 {event_id}：user_id 必須是非空字串")
        text = raw.get("text") or ""
        if not isinstance(text, str):
            raise SchemaError(f"事件 {event_id}：text 必須是字串")

        # 先從原文抽網址再遮罩，避免遮罩改到網址（決策 D6）
        urls = list(self.extract_urls(text))
        return Trigger(
            id=event_id,
            time=_as_datetime(raw.get("time"), event_id),
            source=raw.get("source"),
            group_id=raw.get("group_id"),
            sender_hash=hash_sender(user_id, self._salt) if user_id else "",
            kind="mention" if mention else kind,
            text=self.redact(text),
            urls=urls,
            is_mention=mention,
            reply_token=raw.get("reply_token"),
        )

    # -------------------------------------------------------------- 主流程

    def process(self, raw: Mapping[str, Any]) -> Trigger:
        trigger = self.normalize(raw)
        self.counts["events"] += 1
        self.counts["kind:" + trigger.kind] += 1
        self._trace(trigger, "normalize", f"收到事件（{KIND_LABELS[trigger.kind]}）",
                    kind=trigger.kind, is_mention=trigger.is_mention, url_count=len(trigger.urls))
        observe = getattr(self.fast_filter, "observe", None)
        if callable(observe):
            observe(trigger)  # 例如記住新成員入群的時間

        if trigger.kind not in TEXT_KINDS:
            # 圖片在 Phase 4 加入文字辨識
            self._trace(trigger, "fast_path", "不是文字訊息，只計數")
            return trigger

        signal = self.fast_filter(trigger)
        if not isinstance(signal, RiskSignal):
            raise TypeError("fast_filter 必須回傳 RiskSignal")
        band = signal.band(self.config.thresholds.watch, self.config.thresholds.candidate)
        self.counts["band:" + band] += 1

        message = f"{signal.score} 分 → {BAND_LABELS[band]}"
        if signal.reasons:
            message += f"（{'、'.join(signal.reasons)}）"
        if band == "watch":
            self._record(trigger, signal, outcome="watch")
            message += "，已登記觀察（Thread 和提前行動在 Phase 3 接上）"
        elif band == "candidate":
            self.candidate_queue.append(trigger)
            self._record(trigger, signal, outcome="candidate")
            message += "，已送進候選佇列（Router 在 Phase 3 接上）"
        self._trace(trigger, "fast_path", message, score=signal.score, reasons=list(signal.reasons), band=band)
        return trigger

    def run(self, raw_events: Iterable[Mapping[str, Any]]) -> RunSummary:
        for raw in raw_events:
            self.process(raw)
        return self.summary()

    def summary(self) -> RunSummary:
        by_kind = {key.split(":", 1)[1]: value for key, value in self.counts.items() if key.startswith("kind:")}
        return RunSummary(
            events=self.counts.get("events", 0),
            by_kind=by_kind,
            by_band={band: self.counts.get("band:" + band, 0) for band in BANDS},
            candidates=len(self.candidate_queue),
            records=len(self.records),
            llm=self.llm.stats.as_dict(),
        )

    # -------------------------------------------------------------- 紀錄

    def _trace(self, trigger: Trigger, stage: str, message: str, **data: Any) -> None:
        self.trace.append(TraceEntry(event_id=trigger.id, stage=stage, message=message, data=data))

    def _record(self, trigger: Trigger, signal: RiskSignal, *, outcome: str) -> None:
        """風險事件的最小紀錄：不含訊息內容（SPEC 第 7 節）。"""
        self.records.append(EventRecord(
            event_id=trigger.id,
            time=trigger.time,
            sender_hash=trigger.sender_hash,
            rules=list(signal.reasons),
            risk_level=None,
            outcome=outcome,
        ))
