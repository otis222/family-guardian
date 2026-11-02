"""Watchdog（SPEC 3-7）：從外部監督引擎，每 M 則訊息或每 N 分鐘檢查一次。

只看計數和狀態，不看訊息內容，也不呼叫模型。時間一律用事件時間，不看實際時鐘，回放才對得上。
通知守護人不花推播額度：放進 engine.notices，由今日摘要帶出（決策 D24）。通知只有程式組的固定文字，不含網域或發送者。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .models import GuardianNotice, Trigger

if TYPE_CHECKING:
    from .config import WatchdogSettings
    from .engine import Engine

MESSAGE_KINDS = ("text", "mention", "image")  # 機器人和新成員入群不算訊息
REMINDER_WINDOW = 100
TICK_EVENT_ID = "tick"  # 不是因為某則訊息而做的檢查，決策紀錄用這個編號


@dataclass
class DayUsage:
    """當天的用量。模型統計是累計值，所以記下當天開始時的基準。"""

    day: date
    base_calls: int
    base_errors: int
    base_tokens: int
    messages: int = 0
    replies: int = 0
    reminders: int = 0


class Watchdog:
    def __init__(self, engine: Engine, settings: WatchdogSettings) -> None:
        self.engine = engine
        self.settings = settings
        self.checks = 0
        self.pause_reason: str | None = None
        self.push_used = 0  # LINE 伺服器之後可以用官方額度 API 校正
        self._push_month: tuple[int, int] | None = None
        self._push_warned = False
        self._messages = 0
        self._reminder_marks: deque[int] = deque()  # 每次主動提醒時的訊息序號
        self._since_check = 0
        self._last_check: datetime | None = None
        self._day: DayUsage | None = None
        self._queue_retried = False

    # -------------------------------------------------------------- 引擎的掛勾

    def on_event(self, trigger: Trigger) -> None:
        """引擎每收到一則事件呼叫一次；換日就重算預算，到了檢查時間就檢查。"""
        self._roll_day(trigger.id, trigger.time)
        if trigger.kind in MESSAGE_KINDS:
            self._messages += 1
            self._day.messages += 1
            self._since_check += 1
        if self._last_check is None:
            self._last_check = trigger.time
        if (self._since_check >= self.settings.check_every_messages
                or trigger.time - self._last_check >= timedelta(minutes=self.settings.check_every_minutes)):
            self.check(trigger.id, trigger.time)

    def on_tick(self, now: datetime) -> None:
        """沒有新訊息時也要換日和定時檢查（LINE 伺服器定期呼叫）。"""
        self._roll_day(TICK_EVENT_ID, now)
        if self._last_check is None:
            self._last_check = now
        if now - self._last_check >= timedelta(minutes=self.settings.check_every_minutes):
            self.check(TICK_EVENT_ID, now)

    def on_reply(self, *, proactive: bool) -> None:
        """被 @ 的回應是家人主動問的，不算打擾；主動提醒才算（決策 D25）。"""
        if self._day is not None:
            self._day.replies += 1
            self._day.reminders += proactive
        if proactive:
            self._reminder_marks.append(self._messages)

    def claim_push(self, level: str | None, now: datetime, count: int = 1) -> bool:
        """push 的唯一閘門（SPEC 第 10 節）：只給 high；額度不夠就不送，等今日摘要（決策 D28）。

        count 是收到的人數：LINE 依收件人計算，推給群組要算群組人數。
        """
        self._roll_month(now)
        if level != "high" or self.push_used + count > self.settings.push_quota_monthly:
            return False
        self.push_used += count
        return True

    def sync_push_usage(self, used: int) -> None:
        """用官方額度 API 校正。官方的數字可能還沒算到最新幾則，所以取比較大的。"""
        self.push_used = max(self.push_used, used)

    # -------------------------------------------------------------- 檢查

    def check(self, event_id: str, now: datetime) -> None:
        self.checks += 1
        self._since_check = 0
        self._last_check = now
        self._check_reminders(event_id, now)
        self._check_budget(event_id, now)
        self._check_threads(event_id, now)
        self._check_push_quota(event_id, now)
        self._check_stuck_queue(event_id, now)

    def _check_reminders(self, event_id: str, now: datetime) -> None:
        count = self._recent_reminders()
        limit = self.settings.max_reminders_per_100
        if count > limit and self.engine.speaking_mode == "gentle":
            self.engine.speaking_mode = "silent"
            self._reminder_marks.clear()  # 守護人切回 gentle 時重新計算
            self._act(event_id, now, "too_many_reminders",
                      f"最近 {REMINDER_WINDOW} 則訊息主動提醒了 {count} 次，超過上限 {limit} 次，已切回 silent。"
                      "確認沒問題後可以傳「模式 gentle」。")

    def _check_budget(self, event_id: str, now: datetime) -> None:
        if self.engine.llm.paused:
            return
        calls, errors, tokens = self._day_totals()
        settings = self.settings
        if calls > settings.max_llm_calls_per_day:
            reason = f"今天呼叫模型 {calls} 次，超過預算 {settings.max_llm_calls_per_day} 次"
        elif tokens > settings.max_tokens_per_day:
            reason = f"今天用了 {tokens} token，超過預算 {settings.max_tokens_per_day}"
        elif calls >= settings.min_calls_for_error_rate and errors / calls > settings.max_error_rate:
            reason = f"今天模型錯誤率 {errors / calls:.0%}，超過上限 {settings.max_error_rate:.0%}"
        else:
            return
        self.engine.llm.paused = True
        self.pause_reason = reason
        released = self.engine.release_candidates("paused")
        self._act(event_id, now, "llm_paused",
                  f"{reason}，暫停模型呼叫到明天；這段期間只用規則結果（佇列裡 {released} 則已改用規則結果報告）。")

    def _check_threads(self, event_id: str, now: datetime) -> None:
        for thread in self.engine.threads:
            if thread.verify_count > self.settings.max_thread_verifies:
                self.engine.threads.close(thread.key, block=True)
                self._act(event_id, now, "thread_closed",
                          f"同一個 Thread 查證了 {thread.verify_count} 次還沒結束，已關閉，存活時間內不再開。")

    def _check_push_quota(self, event_id: str, now: datetime) -> None:
        self._roll_month(now)
        quota = self.settings.push_quota_monthly
        if not self._push_warned and self.push_used >= quota * self.settings.push_warn_ratio:
            self._push_warned = True
            self._act(event_id, now, "push_near_quota",
                      f"本月推播用了 {self.push_used}／{quota} 則，接近額度。推播只留給高風險；用完後報告改放今日摘要。")

    def _check_stuck_queue(self, event_id: str, now: datetime) -> None:
        """Router 失敗留下的候選（決策 D16）：重試一次，下次檢查還卡著就改用規則結果報告（決策 D29）。"""
        engine = self.engine
        if not engine.candidate_queue:
            self._queue_retried = False
        elif not engine.has_untried_candidates():
            if self._queue_retried:
                self._queue_retried = False
                released = engine.release_candidates("router_failed")
                self._act(event_id, now, "router_stuck", f"Router 一直失敗，{released} 則候選改用規則結果報告。")
            else:
                self._queue_retried = True
                engine.retry_candidates()
                engine.trace_event(event_id, "watchdog", f"候選卡在佇列（{len(engine.candidate_queue)} 則），重試一次",
                                   kind="router_retry")

    def _act(self, event_id: str, now: datetime, kind: str, message: str) -> None:
        self.engine.add_notice(event_id, GuardianNotice(now, kind, message))

    # -------------------------------------------------------------- 視窗

    def _roll_day(self, event_id: str, when: datetime) -> None:
        today = when.date()
        previous = self._day
        if previous is not None and previous.day == today:
            return
        stats = self.engine.llm.stats
        self._day = DayUsage(today, stats.total_calls, stats.total_errors, stats.prompt_tokens + stats.completion_tokens)
        if previous is not None and self.engine.llm.paused:
            self.engine.llm.paused = False
            self.pause_reason = None
            self._act(event_id, when, "llm_resumed", "新的一天，預算重新計算，恢復呼叫模型。")

    def _roll_month(self, now: datetime) -> None:
        month = (now.year, now.month)
        if self._push_month is not None and month != self._push_month:
            self.push_used = 0
            self._push_warned = False
        self._push_month = month

    def _day_totals(self) -> tuple[int, int, int]:
        stats = self.engine.llm.stats
        tokens = stats.prompt_tokens + stats.completion_tokens
        if self._day is None:
            return stats.total_calls, stats.total_errors, tokens
        day = self._day
        return stats.total_calls - day.base_calls, stats.total_errors - day.base_errors, tokens - day.base_tokens

    def _recent_reminders(self) -> int:
        while self._reminder_marks and self._reminder_marks[0] <= self._messages - REMINDER_WINDOW:
            self._reminder_marks.popleft()
        return len(self._reminder_marks)

    # -------------------------------------------------------------- 給介面和摘要

    def usage_on(self, day: date) -> dict[str, int]:
        if self._day is None or self._day.day != day:
            return {"messages": 0, "replies": 0, "reminders": 0, "llm_calls": 0, "llm_errors": 0, "tokens": 0}
        calls, errors, tokens = self._day_totals()
        return {"messages": self._day.messages, "replies": self._day.replies, "reminders": self._day.reminders,
                "llm_calls": calls, "llm_errors": errors, "tokens": tokens}

    def snapshot(self) -> dict[str, Any]:
        """Watchdog 指標（SPEC 第 9 節的右欄）。"""
        calls, errors, tokens = self._day_totals()
        settings = self.settings
        return {
            "checks": self.checks,
            "speaking_mode": self.engine.speaking_mode,
            "reminders_last_100": self._recent_reminders(),
            "max_reminders_per_100": settings.max_reminders_per_100,
            "llm_calls_today": calls,
            "max_llm_calls_per_day": settings.max_llm_calls_per_day,
            "tokens_today": tokens,
            "max_tokens_per_day": settings.max_tokens_per_day,
            "llm_errors_today": errors,
            "llm_paused": self.engine.llm.paused,
            "pause_reason": self.pause_reason,
            "open_threads": len(self.engine.threads),
            "push_used": self.push_used,
            "push_quota": settings.push_quota_monthly,
            "pending_candidates": len(self.engine.candidate_queue),
            "false_positives": len(self.engine.false_positives),
        }
