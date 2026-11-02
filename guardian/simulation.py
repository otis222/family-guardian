"""模擬器的核心（SPEC 第 9 節）：一則一則跑劇本，把結果整理成純文字；介面（apps/gradio_app.py）只負責顯示。

這裡會顯示訊息原文，因為來源是模擬劇本。真實群組（LINE 伺服器）不用這個模組。
"""

from __future__ import annotations

from pathlib import Path

from .approaches import APPROACH_LABELS, build_engine
from .config import Config
from .llm import LLMClient
from .models import RISK_LABELS, STAGE_LABELS, TraceEntry
from .scenario import Scenario, ScenarioMessage, load_scenario


def format_trace(trace: list[TraceEntry], scenario: Scenario) -> list[str]:
    """決策紀錄的每一行（中欄、命令列 --trace）。只有程式控制的內容，不含訊息原文。"""
    messages = {message.id: message for message in scenario.messages}
    lines = []
    for entry in trace:
        message = messages.get(entry.event_id)
        when = message.time.strftime("%H:%M") if message else "--:--"
        who = scenario.member_name(message.sender) if message else "?"
        lines.append(f"{entry.event_id} {when} {who}｜{STAGE_LABELS.get(entry.stage, entry.stage)}｜{entry.message}")
    return lines


class Simulation:
    def __init__(self, config: Config, scenario: Scenario, approach: str = "C",
                 *, llm_client: LLMClient | None = None) -> None:
        self.config = config
        self.scenario = scenario
        self.approach = approach
        self.engine = build_engine(approach, config, scenario, llm_client=llm_client)
        self.position = 0
        self._chat: list[str] = []
        self._replies_shown = 0

    @classmethod
    def from_path(cls, config: Config, path: str | Path, approach: str = "C", speaking_mode: str | None = None,
                  *, llm_client: LLMClient | None = None) -> Simulation:
        simulation = cls(config, load_scenario(path), approach, llm_client=llm_client)
        if speaking_mode:
            simulation.engine.speaking_mode = speaking_mode
        return simulation

    @property
    def finished(self) -> bool:
        return self.position >= len(self.scenario.messages)

    def step(self) -> bool:
        """處理下一則訊息。回傳後面還有沒有訊息。"""
        if self.finished:
            return False
        message = self.scenario.messages[self.position]
        self._chat.append(self._message_line(message))
        self.engine.process(message.to_raw_event(self.scenario.group_id))
        self.position += 1
        if self.finished:
            self.engine.flush()
        for reply in self.engine.replies[self._replies_shown:]:
            self._chat.append(f"　　顧家貓：{reply.text}{'（附圖卡）' if reply.card else ''}")
        self._replies_shown = len(self.engine.replies)
        return not self.finished

    def _message_line(self, message: ScenarioMessage) -> str:
        who = self.scenario.member_name(message.sender)
        if message.kind == "member_joined":
            return f"{message.time:%m/%d %H:%M} {who} 加入群組"
        return f"{message.time:%m/%d %H:%M} {who}：{message.text}"

    # -------------------------------------------------------------- 各欄的文字

    def status_text(self) -> str:
        total = len(self.scenario.messages)
        state = "，已跑完" if self.finished else ""
        return (f"第 {self.position}／{total} 則{state}｜做法：{APPROACH_LABELS[self.approach]}"
                f"｜發言模式：{self.engine.speaking_mode}｜模型：{self.config.llm_mode}")

    def chat_text(self) -> str:
        return "\n".join(self._chat)

    def trace_text(self) -> str:
        return "\n".join(format_trace(self.engine.trace, self.scenario))

    def replies_text(self) -> str:
        return "\n".join(f"{reply.event_id}（{reply.source}）：{reply.text}" for reply in self.engine.replies)

    def latest_card(self) -> bytes | None:
        return next((reply.card for reply in reversed(self.engine.replies) if reply.card), None)

    def reports_text(self) -> str:
        blocks = []
        for report in self.engine.reports:
            level = f"風險{RISK_LABELS[report.risk_level]}" if report.risk_level else "查證未完成"
            lines = [f"{report.event_id} {report.time:%m/%d %H:%M}｜{level}｜{'即時推播' if report.push else '等今日摘要'}",
                     f"規則：{'、'.join(report.rules) or '無'}", f"摘要：{report.summary}"]
            if report.advice:
                lines.append(f"建議：{'；'.join(report.advice)}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def watchdog_text(self) -> str:
        watch = self.engine.watchdog.snapshot()
        state = f"暫停（{watch['pause_reason']}）" if watch["llm_paused"] else "正常"
        lines = [
            f"檢查 {watch['checks']} 次｜模型呼叫：{state}",
            f"最近 100 則主動提醒：{watch['reminders_last_100']}／{watch['max_reminders_per_100']} 次",
            f"今天模型呼叫：{watch['llm_calls_today']}／{watch['max_llm_calls_per_day']} 次，"
            f"錯誤 {watch['llm_errors_today']} 次",
            f"今天 token：{watch['tokens_today']}／{watch['max_tokens_per_day']}",
            f"訂閱中的 Thread：{watch['open_threads']} 個｜候選佇列：{watch['pending_candidates']} 則",
            f"本月推播：{watch['push_used']}／{watch['push_quota']} 則",
        ]
        if self.engine.notices:
            lines += ["", "給守護人的通知："]
            lines += [f"- {notice.time:%m/%d %H:%M} {notice.message}" for notice in self.engine.notices]
        return "\n".join(lines)
