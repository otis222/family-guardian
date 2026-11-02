"""LINE 伺服器的核心（SPEC 第 5、10 節）：把 webhook 事件交給引擎，再決定怎麼送出去。

這裡不依賴 line-bot-sdk，實際的傳送由 Transport 負責（apps/line_server.py）。

零成本原則：
- 群組回覆一律先用 replyToken，不佔額度。
- 被 @ 但查證來不及：時限內先回「查證中」按鈕，有人點了會帶來新的 replyToken，再用它送結果。
- 其他來不及的情況，只有 high 才用 push，而且要先問 Watchdog 的閘門。
- 給守護人的即時通知只有 high；其他放今日摘要，守護人問的時候用 reply 回。

不保存訊息內容：引擎只留最小紀錄，日誌不寫訊息文字。訊息遮罩、雜湊都在引擎的入口完成。
外部呼叫失敗不重試：記錄下來、維持原狀。

三條執行緒：webhook（enqueue）、工作（process_pending，只有它碰引擎）、期限（check_deadlines）。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from .config import SPEAKING_MODES, LineSettings
from .engine import Engine
from .models import GroupReply, GuardianReport
from .records import RecordStore
from .rules import prepare_text
from .workers.reporter import format_report

logger = logging.getLogger("guardian.line")

TAIPEI = timezone(timedelta(hours=8))
USER_ID = re.compile(r"^U[0-9a-f]{32}$")
GROUP_ID = re.compile(r"^C[0-9a-f]{32}$")
CARD_PATH = "/card/warning.png"
POSTBACK_PREFIX = "result:"
MAX_SEEN_EVENTS = 2000
MAX_ID_SECONDS = 1000
UNKNOWN_SENDER = "unknown-sender"  # LINE 有時不給發送者：這些訊息都算同一個人
MESSAGE_EVENTS = ("message", "messageEdited")  # 被編輯的文字當成新訊息再檢查一次（決策 D59）

CHECKING_TEXT = "顧家貓還在查證，點下面的按鈕看結果。"
CHECKING_LABEL = "看結果"
STILL_CHECKING_TEXT = "還在查證，請等一下再點一次。"
RESULT_EXPIRED_TEXT = "這則查證結果已經過期了。有疑問可以打 165 查證，或先問問家人。"
PAUSED_TEMPLATE = "顧家貓目前暫停中。有疑問可以打 165 查證，或先問問家人。"
HELP_TEXT = "可以用的指令：\n今日摘要\n暫停\n恢復\n模式 silent｜gentle｜on_mention\n誤報 事件編號"
STRANGER_TEXT = "這個帳號只接受守護人的指令。要成為守護人的話，把這串編號給專案作者：\n{user_id}"


class StartupError(RuntimeError):
    """設定不完整，LINE 伺服器不能啟動。訊息只有變數名稱，不含值。"""


@dataclass(frozen=True)
class TextOut:
    text: str


@dataclass(frozen=True)
class ImageOut:
    url: str


@dataclass(frozen=True)
class ButtonOut:
    text: str
    label: str
    data: str


class Transport(Protocol):
    def reply(self, token: str, messages: list[Any]) -> None: ...
    def push(self, to: str, messages: list[Any]) -> None: ...
    def group_size(self, group_id: str) -> int: ...
    def push_usage(self) -> int: ...


@dataclass(frozen=True)
class LineEnv:
    channel_secret: str
    access_token: str
    salt: str
    guardians: tuple[str, ...]
    allowed_groups: frozenset[str]
    public_base_url: str | None


def _id_list(value: str | None, pattern: re.Pattern, name: str) -> list[str]:
    items = [item.strip() for item in (value or "").split(",") if item.strip()]
    for item in items:
        if not pattern.match(item):
            raise StartupError(f"{name} 裡有格式不對的編號（應該是 {pattern.pattern[1:2]} 開頭加 32 個十六進位字元）")
    return items


def load_line_env(env: Mapping[str, str], salt_env: str) -> LineEnv:
    """沒有 GUARDIAN_HASH_SALT 就拒絕啟動（決策 D4）；沒有守護人，報告也沒地方送。"""
    required = {"LINE_CHANNEL_SECRET": env.get("LINE_CHANNEL_SECRET"),
                "LINE_CHANNEL_ACCESS_TOKEN": env.get("LINE_CHANNEL_ACCESS_TOKEN"),
                salt_env: env.get(salt_env), "GUARDIAN_LINE_USER_IDS": env.get("GUARDIAN_LINE_USER_IDS")}
    missing = [name for name, value in required.items() if not (value or "").strip()]
    if missing:
        raise StartupError(f".env 還沒設定：{'、'.join(missing)}")
    public = (env.get("PUBLIC_BASE_URL") or "").strip().rstrip("/") or None
    if public and not public.startswith("https://"):
        raise StartupError("PUBLIC_BASE_URL 必須是 https 網址：LINE 的 webhook 和圖片都要求 HTTPS")
    return LineEnv(
        channel_secret=required["LINE_CHANNEL_SECRET"].strip(),
        access_token=required["LINE_CHANNEL_ACCESS_TOKEN"].strip(),
        salt=required[salt_env].strip(),
        guardians=tuple(_id_list(env.get("GUARDIAN_LINE_USER_IDS"), USER_ID, "GUARDIAN_LINE_USER_IDS")),
        allowed_groups=frozenset(_id_list(env.get("GUARDIAN_GROUP_IDS"), GROUP_ID, "GUARDIAN_GROUP_IDS")),
        public_base_url=public,
    )


@dataclass
class _Pending:
    """一則群組訊息的回覆狀態。replyToken 只在記憶體裡，過期就清掉。"""

    group_id: str
    received_at: datetime
    token: str | None
    mention: bool
    answered: bool = False
    button_sent: bool = False
    result: list[Any] | None = None


@dataclass
class _Item:
    kind: str  # group｜command｜stranger｜postback
    raw: dict[str, Any] | None = None
    event_id: str | None = None
    mention: bool = False
    text: str = ""
    token: str | None = None
    user_id: str | None = None
    data: str = ""


class LineBot:
    def __init__(
        self,
        engine: Engine,
        transport: Transport,
        store: RecordStore,
        *,
        guardians: Iterable[str],
        allowed_groups: Iterable[str],
        settings: LineSettings,
        public_base_url: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.transport = transport
        self.store = store
        self.guardians = tuple(guardians)
        self.allowed_groups = frozenset(allowed_groups)
        self.settings = settings
        self.public_base_url = public_base_url
        self._now = now or (lambda: datetime.now(TAIPEI))
        self.paused = False
        self._ignored_groups: set[Any] = set()
        self._queue: queue.Queue[_Item] = queue.Queue()
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._id_counts: OrderedDict[str, int] = OrderedDict()
        self._retention_day = None
        self._restore()

    def _restore(self) -> None:
        now = self._now()
        records, marked = self.store.load(now)
        self.engine.restore_records(records, marked)
        state = self.store.load_state()
        if state.get("speaking_mode") in SPEAKING_MODES:
            self.engine.speaking_mode = state["speaking_mode"]
        self.paused = state.get("paused") is True
        self._retention_day = now.date()

    def _save_state(self) -> None:
        """存不了也不影響現在的狀態，只是重新啟動後會回到上次存的設定。"""
        try:
            self.store.save_state({"speaking_mode": self.engine.speaking_mode, "paused": self.paused})
        except OSError as exc:
            self.engine.counts["errors:records"] += 1
            logger.error("寫入狀態失敗：%s", type(exc).__name__)

    # -------------------------------------------------------------- webhook 執行緒

    def enqueue(self, events: Iterable[Mapping[str, Any]]) -> None:
        """webhook 要馬上回 200，真正的處理交給工作執行緒。replyToken 從這一刻開始計時。"""
        received_at = self._now()
        for event in events:
            if isinstance(event, Mapping) and not self._is_duplicate(event):
                for item in self._to_items(event, received_at):
                    self._queue.put(item)

    def _is_duplicate(self, event: Mapping[str, Any]) -> bool:
        """LINE 可能重送事件：重複處理會多花模型呼叫，也可能多推一次。"""
        webhook_id = event.get("webhookEventId")
        if not webhook_id:
            return False
        with self._lock:
            if webhook_id in self._seen:
                return True
            self._seen[webhook_id] = None
            while len(self._seen) > MAX_SEEN_EVENTS:
                self._seen.popitem(last=False)
        return False

    def _to_items(self, event: Mapping[str, Any], received_at: datetime) -> list[_Item]:
        source = event.get("source") or {}
        kind = event.get("type")
        if kind == "postback":
            data = str((event.get("postback") or {}).get("data") or "")
            return [_Item("postback", token=event.get("replyToken"), data=data)]
        if source.get("type") == "user":
            return self._direct_message(event, source)
        if source.get("type") != "group":
            return []
        group_id = source.get("groupId")
        if group_id not in self.allowed_groups:
            self._note_unknown_group(group_id, kind)
            return []
        when = self._event_time(event, received_at)
        if kind == "join":
            return [self._group_item(self._raw(when, group_id, None, "join"))]
        if kind == "memberJoined":
            members = (event.get("joined") or {}).get("members") or []
            return [self._group_item(self._raw(when, group_id, member.get("userId") or UNKNOWN_SENDER, "member_joined"))
                    for member in members]
        if kind not in MESSAGE_EVENTS:
            return []
        message = event.get("message") or {}
        user_id = source.get("userId") or UNKNOWN_SENDER
        if message.get("type") == "image":
            return [self._group_item(self._raw(when, group_id, user_id, "image"))]
        if message.get("type") != "text" or not isinstance(message.get("text"), str):
            return []
        mentionees = (message.get("mention") or {}).get("mentionees") or []
        mention = any(item.get("isSelf") is True for item in mentionees)
        raw = self._raw(when, group_id, user_id, "text", text=message["text"], mention=mention)
        token = event.get("replyToken")
        with self._lock:
            self._pending[raw["id"]] = _Pending(group_id, received_at, token, mention)
        return [self._group_item(raw, mention)]

    def _direct_message(self, event: Mapping[str, Any], source: Mapping[str, Any]) -> list[_Item]:
        """守護人的私訊是指令，其他人只會收到固定回覆。私訊內容不送模型、不保存。"""
        message = event.get("message") or {}
        if event.get("type") != "message" or message.get("type") != "text":
            return []
        user_id = source.get("userId")
        kind = "command" if user_id in self.guardians else "stranger"
        return [_Item(kind, text=str(message.get("text") or ""), token=event.get("replyToken"), user_id=user_id)]

    def _note_unknown_group(self, group_id: Any, kind: Any) -> None:
        """不在名單上的群組不處理。編號印在自己電腦的畫面上，守護人確認全員同意後才加進 .env（SPEC 第 7 節）。"""
        with self._lock:
            first = group_id not in self._ignored_groups
            self._ignored_groups.add(group_id)
        if first or kind == "join":
            logger.info("群組 %s 不在 GUARDIAN_GROUP_IDS，事件已忽略。全員同意後把它加進 .env 再重新啟動。", group_id)

    @staticmethod
    def _group_item(raw: dict[str, Any], mention: bool = False) -> _Item:
        return _Item("group", raw=raw, event_id=raw["id"], mention=mention)

    def _event_time(self, event: Mapping[str, Any], received_at: datetime) -> datetime:
        timestamp = event.get("timestamp")
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            return datetime.fromtimestamp(timestamp / 1000, TAIPEI)
        return received_at

    def _raw(self, when: datetime, group_id: str, user_id: str | None, kind: str,
             text: str = "", mention: bool = False) -> dict[str, Any]:
        return {"id": self._new_event_id(when), "time": when, "source": "group", "group_id": group_id,
                "user_id": user_id, "kind": kind, "text": text, "mention": mention}

    def _new_event_id(self, when: datetime) -> str:
        """守護人要在手機上打「誤報 編號」，所以編號要短：月日-時分秒，同一秒再加序號。"""
        base = when.strftime("%m%d-%H%M%S")
        with self._lock:
            count = self._id_counts.get(base, 0) + 1
            self._id_counts[base] = count
            while len(self._id_counts) > MAX_ID_SECONDS:
                self._id_counts.popitem(last=False)
        return base if count == 1 else f"{base}-{count}"

    # -------------------------------------------------------------- 期限執行緒

    def check_deadlines(self) -> None:
        """被 @ 的訊息快到期還沒回答，就先回按鈕（SPEC 第 10 節）。工作執行緒可能正卡在模型呼叫，所以獨立一條。"""
        now = self._now()
        deadline = timedelta(seconds=self.settings.reply_deadline_seconds)
        token_life = timedelta(seconds=self.settings.reply_token_seconds)
        keep = timedelta(hours=self.settings.result_keep_hours)
        due: list[tuple[str, str]] = []
        with self._lock:
            for event_id, pending in list(self._pending.items()):
                age = now - pending.received_at
                if pending.mention and pending.token and not pending.answered and age >= deadline:
                    due.append((event_id, pending.token))
                    pending.token = None
                    pending.button_sent = True
                elif pending.token and age >= token_life:
                    pending.token = None
                if age >= keep:
                    del self._pending[event_id]
        for event_id, token in due:
            button = ButtonOut(CHECKING_TEXT, CHECKING_LABEL, POSTBACK_PREFIX + event_id)
            if not self._safe_reply(token, [button]):
                with self._lock:
                    if event_id in self._pending:
                        self._pending[event_id].button_sent = False

    # -------------------------------------------------------------- 工作執行緒

    def process_pending(self, timeout: float = 0.0) -> None:
        """處理佇列裡的所有事件：推理期間積壓的群組訊息會合併成一批（SPEC 3-3）。沒有事件時往前走一步。"""
        items = self._drain(timeout)
        now = self._now()
        try:
            self._handle(items, now)
        except Exception as exc:  # 一批事件出錯不能讓整個伺服器停掉；只記例外類別，訊息可能帶內容
            self.engine.counts["errors:line_bot"] += 1
            logger.error("處理事件失敗：%s", type(exc).__name__)

    def _drain(self, timeout: float) -> list[_Item]:
        items: list[_Item] = []
        try:
            items.append(self._queue.get(timeout=timeout) if timeout else self._queue.get_nowait())
            while True:
                items.append(self._queue.get_nowait())
        except queue.Empty:
            return items

    def _handle(self, items: list[_Item], now: datetime) -> None:
        group_items = [item for item in items if item.kind == "group"]
        for item in items:
            if item.kind == "command":
                self._run_command(item, now)
            elif item.kind == "stranger":
                self._safe_reply(item.token, [TextOut(STRANGER_TEXT.format(user_id=item.user_id))])
            elif item.kind == "postback":
                self._handle_postback(item)
        if self.paused:
            self._answer_while_paused(group_items)
        elif group_items:
            self.engine.process_batch(item.raw for item in group_items)
        else:
            self.engine.tick(now)
        self._flush(now)
        self._apply_retention(now)

    def _answer_while_paused(self, items: list[_Item]) -> None:
        """暫停時不看訊息、不呼叫模型。被 @ 還是要有回應（SPEC 第 4 節），只回固定文字。"""
        for item in items:
            if item.mention:
                token = self._take_token(item.event_id)
                if token:
                    self._safe_reply(token, [TextOut(PAUSED_TEMPLATE)])

    def _flush(self, now: datetime) -> None:
        replies, self.engine.replies = self.engine.replies, []
        for reply in replies:
            self._deliver(reply, now)
        reports, self.engine.reports = self.engine.reports, []
        for report in reports:
            if report.push:
                self._push_report(report)
        try:
            self.store.save(self.engine.records)
        except OSError as exc:
            self.engine.counts["errors:records"] += 1
            logger.error("寫入風險事件紀錄失敗：%s", type(exc).__name__)
        self.engine.trim_memory()

    def _apply_retention(self, now: datetime) -> None:
        if now.date() == self._retention_day:
            return
        self._retention_day = now.date()
        self.engine.forget_before(now - self.store.retention)
        try:
            self.store.rewrite(self.engine.records, self.engine.false_positives)
        except OSError as exc:
            self.engine.counts["errors:records"] += 1
            logger.error("清理過期紀錄失敗：%s", type(exc).__name__)

    # -------------------------------------------------------------- 送出去

    def _reply_messages(self, reply: GroupReply) -> list[Any]:
        messages: list[Any] = [TextOut(reply.text)]
        if reply.card and self.public_base_url:  # 圖卡要有 HTTPS 網址；沒有通道時只送文字
            messages.append(ImageOut(self.public_base_url + CARD_PATH))
        return messages

    def _take_token(self, event_id: str | None) -> str | None:
        """replyToken 只能用一次：拿走的同時標記成已回答，期限執行緒就不會再回按鈕。"""
        with self._lock:
            pending = self._pending.get(event_id)
            if pending is None:
                return None
            age = self._now() - pending.received_at
            token = pending.token if age < timedelta(seconds=self.settings.reply_token_seconds) else None
            pending.token = None
            pending.answered = True
            return token

    def _deliver(self, reply: GroupReply, now: datetime) -> None:
        messages = self._reply_messages(reply)
        token = self._take_token(reply.event_id)
        if token and self._safe_reply(token, messages):
            return
        with self._lock:
            pending = self._pending.get(reply.event_id)
            if pending and pending.button_sent:
                pending.result = messages  # 等有人點按鈕，用新的 replyToken 送
                return
            group_id = pending.group_id if pending else None
        self._push_to_group(reply.event_id, group_id, messages, now)

    def _push_to_group(self, event_id: str, group_id: str | None, messages: list[Any], now: datetime) -> None:
        """不得已才用 push：只給 high，而且 LINE 依群組人數計算。"""
        if group_id is None or self.engine.risk_level_of(event_id) != "high":
            self.engine.counts["line:reply_dropped"] += 1
            return
        try:
            size = self.transport.group_size(group_id)
            if self.engine.watchdog.claim_push("high", now, count=size):
                self.transport.push(group_id, messages)
            else:
                self.engine.counts["line:reply_dropped"] += 1
        except Exception as exc:
            self.engine.counts["errors:line_push"] += 1
            logger.error("推播到群組失敗：%s", type(exc).__name__)

    def _push_report(self, report: GuardianReport) -> None:
        """Watchdog 的通知搭這次推播順便送，不多花額度（決策 D24）。"""
        text = format_report(report)
        unread = [notice for notice in self.engine.notices if not notice.delivered]
        if unread:
            text += "\n\nWatchdog 通知：\n" + "\n".join(f"- {notice.message}" for notice in unread)
        for guardian in self.guardians:
            try:
                self.transport.push(guardian, [TextOut(text[:5000])])
            except Exception as exc:
                self.engine.counts["errors:line_push"] += 1
                logger.error("推播報告失敗：%s", type(exc).__name__)
                return
        for notice in unread:
            notice.delivered = True

    def _safe_reply(self, token: str | None, messages: list[Any]) -> bool:
        if not token:
            return False
        try:
            self.transport.reply(token, messages)
            return True
        except Exception as exc:
            self.engine.counts["errors:line_reply"] += 1
            logger.error("回覆失敗：%s", type(exc).__name__)
            return False

    # -------------------------------------------------------------- 按鈕和指令

    def _handle_postback(self, item: _Item) -> None:
        if not item.data.startswith(POSTBACK_PREFIX):
            return
        with self._lock:
            pending = self._pending.get(item.data[len(POSTBACK_PREFIX):])
            result = pending.result if pending else None
            waiting = bool(pending and pending.button_sent)
        if result:
            self._safe_reply(item.token, result)
        elif waiting:
            self._safe_reply(item.token, [TextOut(STILL_CHECKING_TEXT)])
        else:
            self._safe_reply(item.token, [TextOut(RESULT_EXPIRED_TEXT)])

    def _run_command(self, item: _Item, now: datetime) -> None:
        self._safe_reply(item.token, [TextOut(self.run_command(item.text, now))])

    def run_command(self, text: str, now: datetime) -> str:
        """守護人的管理指令（SPEC 第 5 節）。回傳要回覆的文字。"""
        words = prepare_text(text).split()
        name = words[0] if words else ""
        argument = " ".join(words[1:])
        if name.startswith("模式") and not argument:
            name, argument = "模式", name[2:]
        if name.startswith("誤報") and not argument:
            name, argument = "誤報", name[2:]
        if name == "今日摘要" and not argument:
            return self._daily_summary(now)
        if name == "暫停" and not argument:
            return self._pause()
        if name == "恢復" and not argument:
            return self._resume()
        if name == "模式":
            return self._set_mode(argument.lower())
        if name == "誤報":
            return self._mark_false_positive(argument)
        return HELP_TEXT

    def _daily_summary(self, now: datetime) -> str:
        try:
            self.engine.watchdog.sync_push_usage(self.transport.push_usage())
        except Exception as exc:
            logger.error("查詢推播用量失敗：%s", type(exc).__name__)
        paused = "顧家貓目前暫停中。\n" if self.paused else ""
        return paused + self.engine.daily_summary(now.date())

    def _pause(self) -> str:
        self.paused = True
        self.engine.release_candidates("paused")  # 佇列裡的候選不再呼叫模型，改用規則結果報告
        self._save_state()
        return "顧家貓暫停了：不看群組訊息、不呼叫模型，被 @ 時只回固定文字。要恢復請傳「恢復」。"

    def _resume(self) -> str:
        self.paused = False
        self._save_state()
        return f"顧家貓恢復了。發言模式：{self.engine.speaking_mode}。"

    def _set_mode(self, mode: str) -> str:
        if mode not in SPEAKING_MODES:
            return f"模式只能是 {'、'.join(SPEAKING_MODES)}。"
        self.engine.speaking_mode = mode
        self._save_state()
        return f"發言模式改成 {mode} 了。"

    def _mark_false_positive(self, event_id: str) -> str:
        if not event_id:
            return "請傳「誤報 事件編號」，編號在報告的最後一行。"
        if not self.engine.mark_false_positive(event_id):
            return "找不到這個事件編號。編號在報告的最後一行。"
        try:
            self.store.add_false_positive(event_id)
        except OSError as exc:
            self.engine.counts["errors:records"] += 1
            logger.error("寫入誤報紀錄失敗：%s", type(exc).__name__)
        return f"收到，事件 {event_id} 標記成誤報了。"
