"""顧家貓的資料結構（SPEC 3-1 到 3-5、第 7 節）。

這裡只放資料和格式檢查，不放流程邏輯。檢查失敗一律丟出 SchemaError（ValueError 的子類別），
訊息用繁體中文，方便直接顯示在命令列或 Gradio。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

SOURCES = ("group", "user")
KINDS = ("text", "image", "join", "member_joined", "mention")
ACTIONS = ("ignore", "watch", "verify")
RISK_LEVELS = ("low", "medium", "high")
BANDS = ("ignore", "watch", "candidate")
THREAD_KEY_TYPES = ("sender", "domain")

# 給人看的名稱（決策紀錄、命令列摘要）
KIND_LABELS = {
    "text": "文字",
    "image": "圖片",
    "join": "機器人入群",
    "member_joined": "新成員入群",
    "mention": "提及顧家貓",
}
BAND_LABELS = {"ignore": "不處理", "watch": "觀察", "candidate": "候選"}
RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}

# 風險事件的最小紀錄只能有這些欄位（SPEC 第 7 節）
RECORD_FIELDS = ("event_id", "time", "sender_hash", "rules", "risk_level", "outcome")


class SchemaError(ValueError):
    """資料或模型輸出不符合固定格式。"""


def _require_str(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{name} 必須是字串")
    if not allow_empty and not value.strip():
        raise SchemaError(f"{name} 不能是空的")
    return value


def _require_choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise SchemaError(f"{name} 必須是 {'、'.join(choices)} 其中之一，收到：{value!r}")
    return value


def _require_str_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SchemaError(f"{name} 必須是字串清單")
    return list(value)


def _require_aware(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise SchemaError(f"{name} 必須是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SchemaError(f"{name} 必須帶時區，例如 2026-11-08T08:00:00+08:00")
    return value


def _require_keys(data: Any, keys: tuple[str, ...], name: str) -> dict:
    if not isinstance(data, dict):
        raise SchemaError(f"{name}必須是 JSON 物件")
    missing = [key for key in keys if key not in data]
    if missing:
        raise SchemaError(f"{name}缺少欄位：{'、'.join(missing)}")
    return data


@dataclass
class Trigger:
    """正規化後的事件（SPEC 3-1）。

    text 是遮罩過的內容，只能留在記憶體裡（例如組 prompt），不能寫進任何紀錄。
    kind 是 mention 時代表文字訊息提及了顧家貓，is_mention 必須同時是 True。
    """

    id: str
    time: datetime
    source: str
    group_id: str | None
    sender_hash: str
    kind: str
    text: str = ""
    urls: list[str] = field(default_factory=list)
    is_mention: bool = False
    # 只存在記憶體：不進 repr、不進 to_dict、不寫入任何紀錄
    reply_token: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_str(self.id, "Trigger.id")
        _require_aware(self.time, "Trigger.time")
        _require_choice(self.source, "Trigger.source", SOURCES)
        _require_choice(self.kind, "Trigger.kind", KINDS)
        if self.source == "group":
            _require_str(self.group_id, "群組事件的 Trigger.group_id")
        # 機器人入群（join）沒有發送者，其他事件都要有
        _require_str(self.sender_hash, "Trigger.sender_hash", allow_empty=self.kind == "join")
        _require_str(self.text, "Trigger.text", allow_empty=True)
        self.urls = _require_str_list(self.urls, "Trigger.urls")
        if not isinstance(self.is_mention, bool):
            raise SchemaError("Trigger.is_mention 必須是 True 或 False")
        if (self.kind == "mention") != self.is_mention:
            raise SchemaError("Trigger.kind 是 mention 時 is_mention 必須是 True，反之亦然")

    def to_dict(self) -> dict[str, Any]:
        """記憶體內使用（組 prompt、介面顯示）。不含 reply_token；含已遮罩的 text，不能寫進紀錄。"""
        return {
            "id": self.id,
            "time": self.time.isoformat(),
            "source": self.source,
            "group_id": self.group_id,
            "sender_hash": self.sender_hash,
            "kind": self.kind,
            "text": self.text,
            "urls": list(self.urls),
            "is_mention": self.is_mention,
        }


@dataclass
class RiskSignal:
    """快速過濾的輸出（SPEC 3-2）。

    reasons 只能寫規則名稱（例如「要求轉帳」），不能引用訊息原文，因為它會寫進風險事件紀錄。
    """

    score: int
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, int) or self.score < 0:
            raise SchemaError("RiskSignal.score 必須是 0 以上的整數")
        self.reasons = _require_str_list(self.reasons, "RiskSignal.reasons")

    def band(self, watch: int, candidate: int) -> str:
        """依兩個門檻分三段：低於 watch 不處理；watch 到 candidate 之間觀察；達到 candidate 進候選佇列。"""
        if not 0 <= watch < candidate:
            raise SchemaError("門檻必須符合 0 ≤ watch < candidate")
        if self.score < watch:
            return "ignore"
        if self.score < candidate:
            return "watch"
        return "candidate"


@dataclass(frozen=True)
class Decision:
    """Router 對一個候選事件的決策（SPEC 3-3）。"""

    event_id: str
    action: str
    reason: str

    def __post_init__(self) -> None:
        _require_str(self.event_id, "Decision.event_id")
        _require_choice(self.action, "Decision.action", ACTIONS)
        _require_str(self.reason, "Decision.reason")

    @classmethod
    def from_dict(cls, data: Any) -> Decision:
        data = _require_keys(data, ("event_id", "action", "reason"), "決策")
        return cls(event_id=data["event_id"], action=data["action"], reason=data["reason"])

    def to_dict(self) -> dict[str, str]:
        return {"event_id": self.event_id, "action": self.action, "reason": self.reason}


def parse_decisions(data: Any) -> list[Decision]:
    """把 Router 的輸出（已解析的 JSON）轉成決策清單。

    批次處理時一次會有多個決策，所以接受單一物件、物件清單，或 {"decisions": [...]}。
    """
    if isinstance(data, dict) and "decisions" in data:
        data = data["decisions"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise SchemaError("Router 的輸出必須是決策物件或決策清單")
    return [Decision.from_dict(item) for item in data]


@dataclass
class VerifyResult:
    """查證 Worker 的輸出（SPEC 3-4）。"""

    risk_level: str
    reasons: list[str]
    evidence: list[str]
    advice: list[str]

    def __post_init__(self) -> None:
        _require_choice(self.risk_level, "VerifyResult.risk_level", RISK_LEVELS)
        self.reasons = _require_str_list(self.reasons, "VerifyResult.reasons")
        self.evidence = _require_str_list(self.evidence, "VerifyResult.evidence")
        self.advice = _require_str_list(self.advice, "VerifyResult.advice")

    @classmethod
    def from_dict(cls, data: Any) -> VerifyResult:
        data = _require_keys(data, ("risk_level", "reasons", "evidence", "advice"), "查證結果")
        return cls(
            risk_level=data["risk_level"],
            reasons=data["reasons"],
            evidence=data["evidence"],
            advice=data["advice"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_level": self.risk_level,
            "reasons": list(self.reasons),
            "evidence": list(self.evidence),
            "advice": list(self.advice),
        }


@dataclass
class Thread:
    """觀察中的對話串（SPEC 3-5）。key 是 (group_id, sender_hash) 或 (group_id, domain)。"""

    key: tuple[str, str]
    key_type: str
    created_at: datetime
    expires_at: datetime
    worker: str = "verifier"
    event_ids: list[str] = field(default_factory=list)
    verify_count: int = 0  # 查證次數，給 Watchdog 判斷鬼打牆（SPEC 3-7）

    def __post_init__(self) -> None:
        if (
            not isinstance(self.key, tuple)
            or len(self.key) != 2
            or not all(isinstance(part, str) and part for part in self.key)
        ):
            raise SchemaError("Thread.key 必須是 (group_id, sender_hash) 或 (group_id, domain)")
        _require_choice(self.key_type, "Thread.key_type", THREAD_KEY_TYPES)
        _require_aware(self.created_at, "Thread.created_at")
        _require_aware(self.expires_at, "Thread.expires_at")
        if self.expires_at <= self.created_at:
            raise SchemaError("Thread.expires_at 必須晚於 created_at")
        _require_str(self.worker, "Thread.worker")
        self.event_ids = _require_str_list(self.event_ids, "Thread.event_ids")

    @classmethod
    def open(
        cls,
        *,
        group_id: str,
        value: str,
        key_type: str,
        now: datetime,
        ttl_hours: int = 24,
        worker: str = "verifier",
    ) -> Thread:
        return cls(
            key=(group_id, value),
            key_type=key_type,
            created_at=now,
            expires_at=now + timedelta(hours=ttl_hours),
            worker=worker,
        )

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass
class EventRecord:
    """風險事件的最小紀錄（SPEC 第 7 節）。

    只有時間、雜湊後的發送者、命中的規則、風險等級、處理結果，加上事件編號（給「誤報 <事件編號>」用）。
    不含訊息內容、網址、原始 ID 或 reply_token。
    """

    event_id: str
    time: datetime
    sender_hash: str
    rules: list[str]
    risk_level: str | None  # 還沒查證時是 None
    outcome: str

    def __post_init__(self) -> None:
        _require_str(self.event_id, "EventRecord.event_id")
        _require_aware(self.time, "EventRecord.time")
        _require_str(self.sender_hash, "EventRecord.sender_hash", allow_empty=True)
        self.rules = _require_str_list(self.rules, "EventRecord.rules")
        if self.risk_level is not None:
            _require_choice(self.risk_level, "EventRecord.risk_level", RISK_LEVELS)
        _require_str(self.outcome, "EventRecord.outcome")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "time": self.time.isoformat(),
            "sender_hash": self.sender_hash,
            "rules": list(self.rules),
            "risk_level": self.risk_level,
            "outcome": self.outcome,
        }


@dataclass
class GroupReply:
    """顧家貓要在群組說的話（SPEC 第 4 節）。source：model｜rewrite｜template。card 是警示圖卡的 PNG。"""

    event_id: str
    text: str
    source: str
    card: bytes | None = field(default=None, repr=False)


@dataclass
class GuardianReport:
    """給守護人的結構化報告（SPEC 3-4）。不含訊息內容和網址；只有 high 才即時推播（SPEC 第 10 節）。"""

    event_id: str
    time: datetime
    sender_hash: str
    risk_level: str | None  # 查證失敗時是 None
    rules: list[str]
    reasons: list[str]
    evidence: list[str]
    advice: list[str]
    summary: str
    push: bool = False


@dataclass
class GuardianNotice:
    """Watchdog 給守護人的通知（SPEC 3-7）。不推播，由今日摘要帶出（決策 D24）。不含訊息內容、網域或發送者。"""

    time: datetime
    kind: str
    message: str
    delivered: bool = False


@dataclass
class TraceEntry:
    """決策紀錄的一行（Gradio 中欄、命令列 --trace）。只在記憶體裡，也不放訊息原文和模型的文字。"""

    event_id: str
    stage: str  # normalize、fast_path、thread、router、verifier、early_action、reflection、card、speak、reporter、watchdog
    message: str
    data: dict[str, Any] = field(default_factory=dict)
