"""引擎（SPEC 第 3 節）：原始事件 → Trigger → 快速過濾 → Thread／提前行動／Router → Worker → 發言與回報。

原始事件的格式（模擬劇本和 LINE 伺服器都轉成這個格式再交給引擎）：
    {"id", "time", "source", "group_id", "user_id", "kind", "text", "mention", "reply_token"}
user_id 一進引擎就雜湊成 sender_hash，原始值不保存。

決策紀錄只記程式控制的內容（動作、等級、工具名稱、問題類別、例外類別），模型的文字不進紀錄（決策 D21）。
圖卡和 Watchdog 在 Phase 4 接上。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from .config import Config
from .data_165 import RumorIndex
from .llm import LLMClient, LLMError
from .models import (
    BAND_LABELS,
    BANDS,
    KIND_LABELS,
    RISK_LABELS,
    Decision,
    EventRecord,
    GroupReply,
    GuardianReport,
    RiskSignal,
    SchemaError,
    TraceEntry,
    Trigger,
)
from .redact import redact as default_redact
from .router import NEUTRAL_TEMPLATE, UNVERIFIED_TEMPLATE, Candidate, Router, should_speak
from .rules import BLOCKLIST_REASON, FastFilter
from .threads import ThreadStore
from .urls import extract_urls as default_extract_urls
from .workers.reporter import build_failure_report, build_report
from .workers.verifier import Verifier

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
    candidates: int  # 進過候選佇列的事件數
    pending: int  # 還留在佇列裡的事件數（Router 失敗時）
    router_rounds: int
    records: int
    reports: int
    replies: int
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
        rumors: RumorIndex | None = None,
    ) -> None:
        if not isinstance(salt, str) or not salt:
            raise ValueError("Engine 需要雜湊用的鹽：模擬劇本用 DEMO_SALT，LINE 伺服器用 .env 的 GUARDIAN_HASH_SALT")
        self.config = config
        self._salt = salt
        self.llm = llm_client or LLMClient.from_config(config)
        self.redact = redact or default_redact
        self.extract_urls = extract_urls or default_extract_urls
        self.fast_filter = fast_filter or FastFilter.from_config(config)
        base = self.fast_filter if isinstance(self.fast_filter, FastFilter) else FastFilter.from_config(config)
        self.rules = base.rules
        if rumors is None:
            rumors = RumorIndex.from_cache(config.data_165.cache_dir, config.data_165.rumors)
        self.verifier = Verifier(self.rules, base.blocklist, rumors, self.llm)
        self.router = Router(self.llm, config.max_reply_chars)
        self.threads = ThreadStore(config.thread_ttl_hours)
        self.speaking_mode = config.speaking_mode
        self.trace: list[TraceEntry] = []
        self.records: list[EventRecord] = []
        self.reports: list[GuardianReport] = []
        self.replies: list[GroupReply] = []
        self.decisions: list[Decision] = []  # 含模型的理由，只留在記憶體、不進決策紀錄
        self.candidate_queue: list[Candidate] = []
        self.counts: Counter[str] = Counter()
        self._records_by_id: dict[str, EventRecord] = {}
        self._queue_has_new = False  # 佇列裡有還沒試過的事件；失敗過的事件不會因為之後的閒聊被重試（決策 D16）
        self._router_busy_until: datetime | None = None
        self._last_time: datetime | None = None

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
        self._last_time = trigger.time
        self.counts["events"] += 1
        self.counts["kind:" + trigger.kind] += 1
        self._trace(trigger, "normalize", f"收到事件（{KIND_LABELS[trigger.kind]}）",
                    kind=trigger.kind, is_mention=trigger.is_mention, url_count=len(trigger.urls))
        self.threads.expire(trigger.time)
        self._run_router_if_due(trigger.time)
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
        self._trace(trigger, "fast_path", message, score=signal.score, reasons=list(signal.reasons), band=band)
        if band != "ignore":
            self._record(trigger, signal, outcome=band)

        self._route(trigger, signal, band)
        self._run_router_if_due(trigger.time)
        return trigger

    def _route(self, trigger: Trigger, signal: RiskSignal, band: str) -> None:
        thread = self.threads.find(trigger)
        # 直通跳過 Router，但只送有風險訊號的訊息：0 分的閒聊不送模型（決策 D10）
        if thread and (thread.key_type == "domain" or signal.score > 0):
            thread.event_ids.append(trigger.id)
            thread.verify_count += 1
            self._trace(trigger, "thread", f"屬於訂閱中的 Thread（{thread.key_type}），直通查證，不經過 Router",
                        key_type=thread.key_type, verify_count=thread.verify_count)
            self._verify_and_act(trigger, signal, "thread")
        elif band == "candidate" and BLOCKLIST_REASON in signal.reasons:
            self._trace(trigger, "router", "165 名單命中，程式直接查證，不問模型", action="verify")
            self._verify_and_act(trigger, signal, "router")
        elif band == "candidate":
            self.counts["candidates"] += 1
            self.candidate_queue.append(Candidate(trigger, signal, self.rules.flags_injection(trigger.text)))
            self._queue_has_new = True
            self._trace(trigger, "router", "進候選佇列")
        elif band == "watch":
            self._open_thread(trigger)
            self._verify_and_act(trigger, signal, "early")
        else:
            if thread:
                self.counts["thread_skipped"] += 1
                self._trace(trigger, "thread", "Thread 內的 0 分訊息，只計數，不送模型")
            if trigger.is_mention:
                self._reply_template(trigger, NEUTRAL_TEMPLATE)

    def run(self, raw_events: Iterable[Mapping[str, Any]]) -> RunSummary:
        for raw in raw_events:
            self.process(raw)
        self.flush()
        return self.summary()

    def flush(self) -> None:
        """不等 Router 忙完，把新進的候選送出去（劇本跑完時用）。"""
        if self._queue_has_new and self._last_time is not None:
            self._run_router_round(self._last_time)

    def summary(self) -> RunSummary:
        by_kind = {key.split(":", 1)[1]: value for key, value in self.counts.items() if key.startswith("kind:")}
        return RunSummary(
            events=self.counts.get("events", 0),
            by_kind=by_kind,
            by_band={band: self.counts.get("band:" + band, 0) for band in BANDS},
            candidates=self.counts.get("candidates", 0),
            pending=len(self.candidate_queue),
            router_rounds=self.counts.get("router_rounds", 0),
            records=len(self.records),
            reports=len(self.reports),
            replies=len(self.replies),
            llm=self.llm.stats.as_dict(),
        )

    # -------------------------------------------------------------- Router（批次處理，決策 D15）

    def _run_router_if_due(self, now: datetime) -> None:
        if self._queue_has_new and (self._router_busy_until is None or now >= self._router_busy_until):
            self._run_router_round(now)

    def _run_router_round(self, now: datetime) -> None:
        batch, self.candidate_queue = self.candidate_queue, []
        self._queue_has_new = False
        self._router_busy_until = now + timedelta(seconds=self.config.router_latency_seconds)
        self.counts["router_rounds"] += 1
        try:
            results = self.router.decide(batch)
        except (LLMError, SchemaError) as exc:
            self._router_failed(batch, exc)
            return
        for item, decision, overridden in results:
            self.decisions.append(decision)
            note = "（程式覆蓋模型的決定）" if overridden else ""
            self._trace(item.trigger, "router", f"{decision.action}（批次 {len(batch)} 則）{note}",
                        action=decision.action, batch_size=len(batch), overridden=overridden)
            self._act_on_decision(item, decision)

    def _router_failed(self, batch: list[Candidate], exc: Exception) -> None:
        """維持原狀、記錄錯誤、不重試（決策 D16）。被 @ 的事件要趁 replyToken 有效時回應，先用模板回覆。"""
        self.counts["errors:router"] += 1
        for item in batch:
            self._trace(item.trigger, "router", f"Router 失敗（{type(exc).__name__}），未重試",
                        error=type(exc).__name__, batch_size=len(batch))
            if item.trigger.is_mention:
                self._finish(item.trigger, item.signal, None, "router_failed")
                self._reply_template(item.trigger, UNVERIFIED_TEMPLATE)
            else:
                self.candidate_queue.append(item)

    def _act_on_decision(self, item: Candidate, decision: Decision) -> None:
        trigger, signal = item.trigger, item.signal
        if decision.action == "verify":
            self._verify_and_act(trigger, signal, "router")
            return
        if decision.action == "watch":
            self._open_thread(trigger)
            self._finish(trigger, signal, None, "watching")
        else:
            self._finish(trigger, signal, None, "ignored")
        if trigger.is_mention:
            self._reply_template(trigger, NEUTRAL_TEMPLATE)

    # -------------------------------------------------------------- Worker 和發言

    def _open_thread(self, trigger: Trigger) -> None:
        thread = self.threads.open(trigger)
        if thread:
            thread.event_ids.append(trigger.id)
            self._trace(trigger, "thread", f"建立或沿用 Thread（{thread.key_type}）", key_type=thread.key_type)

    def _verify_and_act(self, trigger: Trigger, signal: RiskSignal, path: str) -> None:
        """path：router（候選）｜thread（訂閱直通）｜early（提前行動，只有 high 才往下走）。"""
        flagged = self.rules.flags_injection(trigger.text)
        outcome = self.verifier.verify(trigger, signal, flagged=flagged)
        data = {"calls": outcome.calls, "tools": list(outcome.tools), "path": path}
        if outcome.result is None:
            self.counts["errors:verifier"] += 1
            self._trace(trigger, "verifier", f"查證失敗（{outcome.error}），未重試", error=outcome.error, **data)
            self._finish(trigger, signal, None, "verify_failed")
            if path != "early":
                self._add_report(trigger, build_failure_report(trigger, signal))
            if trigger.is_mention:
                self._reply_template(trigger, UNVERIFIED_TEMPLATE)
            return

        result = outcome.result
        level = result.risk_level
        self._trace(trigger, "verifier",
                    f"風險{RISK_LABELS[level]}（模型 {outcome.calls} 次；工具：{'、'.join(outcome.tools)}）",
                    risk_level=level, flagged=flagged, **data)
        if path == "early":
            kept = level == "high"
            self._trace(trigger, "early_action", "結果是高風險，交給 Router" if kept else "結果不是高風險，丟棄",
                        risk_level=level, kept=kept)
        if level == "low" or (path == "early" and level != "high"):
            self._finish(trigger, signal, level, "discarded" if path == "early" else "verified_low")
            if trigger.is_mention:
                self._reply_template(trigger, NEUTRAL_TEMPLATE)
            return

        self._add_report(trigger, build_report(trigger, signal, result, client=self.llm, used_model=outcome.calls > 0))
        speak = should_speak(self.speaking_mode, level, trigger.is_mention)
        if speak:
            reply, notes = self.router.compose_reply(trigger.id, level, list(signal.reasons))
            for note in notes:
                self._trace(trigger, "reflection", note)
            self._add_reply(trigger, reply)
        else:
            self._trace(trigger, "speak", f"{self.speaking_mode} 模式，不在群組發言", mode=self.speaking_mode)
        self._finish(trigger, signal, level, "reported_replied" if speak else "reported")

    def _add_report(self, trigger: Trigger, report: GuardianReport) -> None:
        self.reports.append(report)
        self._trace(trigger, "reporter", "報告守護人（即時）" if report.push else "報告守護人（等每日摘要）",
                    push=report.push, risk_level=report.risk_level)

    def _add_reply(self, trigger: Trigger, reply: GroupReply) -> None:
        self.replies.append(reply)
        self._trace(trigger, "speak", f"群組回覆（{reply.source}，{len(reply.text)} 字）",
                    source=reply.source, length=len(reply.text))

    def _reply_template(self, trigger: Trigger, text: str) -> None:
        self._add_reply(trigger, GroupReply(trigger.id, text, "template"))

    # -------------------------------------------------------------- 紀錄

    def _trace(self, trigger: Trigger, stage: str, message: str, **data: Any) -> None:
        self.trace.append(TraceEntry(event_id=trigger.id, stage=stage, message=message, data=data))

    def _record(self, trigger: Trigger, signal: RiskSignal, *, outcome: str) -> EventRecord:
        """風險事件的最小紀錄：不含訊息內容（SPEC 第 7 節）。"""
        record = EventRecord(
            event_id=trigger.id,
            time=trigger.time,
            sender_hash=trigger.sender_hash,
            rules=list(signal.reasons),
            risk_level=None,
            outcome=outcome,
        )
        self.records.append(record)
        self._records_by_id[trigger.id] = record
        return record

    def _finish(self, trigger: Trigger, signal: RiskSignal, level: str | None, outcome: str) -> None:
        """更新事件的處理結果。Thread 直通的低分訊息沒有紀錄：查出風險或查證失敗才新增。"""
        record = self._records_by_id.get(trigger.id)
        if record is None:
            if level == "low":
                return
            record = self._record(trigger, signal, outcome=outcome)
        record.risk_level = level
        record.outcome = outcome
