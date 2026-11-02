"""回報 Worker（SPEC 3-4，決策 D19、D20）：給守護人的結構化報告。

欄位由程式組好，不含訊息內容；模型文字裡的網址換成＜網址＞，避免守護人誤點。
只有模型查證過的 high 才請模型寫一句摘要（high 才會即時推播，SPEC 第 10 節），其他都用模板，不花 token。
"""

from __future__ import annotations

from datetime import date
from typing import Collection, Mapping

from ..llm import LLMClient, LLMError, call_llm
from ..models import RISK_LABELS, EventRecord, GuardianNotice, GuardianReport, RiskSignal, Trigger, VerifyResult
from ..prompts import REPORTER_SYSTEM, reporter_prompt
from ..urls import strip_urls

MAX_SUMMARY_CHARS = 200
MAX_LISTED_EVENTS = 20
MAX_DAILY_SUMMARY_CHARS = 4900  # LINE 文字訊息上限是 5000 字
UNFINISHED_OUTCOMES = ("candidate", "verify_failed", "router_failed", "paused")


def _template(level: str, rules: list[str]) -> str:
    return f"風險{RISK_LABELS[level]}：{'、'.join(rules) or '查證結果'}。詳情見報告欄位。"


def build_report(
    trigger: Trigger, signal: RiskSignal, result: VerifyResult, *, client: LLMClient, used_model: bool
) -> GuardianReport:
    level = result.risk_level
    rules = list(signal.reasons)
    reasons, evidence, advice = ([strip_urls(item) for item in items]
                                 for items in (result.reasons, result.evidence, result.advice))
    summary = _template(level, rules)
    if level == "high" and used_model:
        try:
            text = call_llm("reporter", REPORTER_SYSTEM, reporter_prompt(level, rules, reasons, advice), client=client)
            summary = strip_urls(text).strip()[:MAX_SUMMARY_CHARS]
        except LLMError:
            pass  # 摘要失敗就用模板，報告照樣送出；錯誤已計入 LLMStats
    return GuardianReport(trigger.id, trigger.time, trigger.sender_hash, level, rules,
                          reasons, evidence, advice, summary, push=level == "high")


def build_failure_report(trigger: Trigger, signal: RiskSignal, *, reason: str = "查證沒有完成") -> GuardianReport:
    """查證失敗或模型暫停時也要讓守護人知道，只有規則比對結果。"""
    rules = list(signal.reasons)
    summary = f"{reason}，只有規則比對結果：{'、'.join(rules) or '無'}。"
    return GuardianReport(trigger.id, trigger.time, trigger.sender_hash, None, rules, [], [], [], summary, push=False)


def format_report(report: GuardianReport) -> str:
    """推給守護人的文字（SPEC 3-4）。不含訊息內容和網址；最後一行方便標記誤報。"""
    level = f"風險{RISK_LABELS[report.risk_level]}" if report.risk_level else "查證未完成"
    lines = [f"顧家貓報告（{report.time:%m/%d %H:%M}）｜{level}",
             f"規則：{'、'.join(report.rules) or '無'}", f"摘要：{report.summary}"]
    if report.reasons:
        lines.append(f"理由：{'；'.join(report.reasons)}")
    if report.advice:
        lines.append(f"建議：{'；'.join(report.advice)}")
    lines.append(f"事件編號：{report.event_id}（誤報請傳「誤報 {report.event_id}」）")
    return "\n".join(lines)[:MAX_DAILY_SUMMARY_CHARS]


def _event_lines(title: str, records: list[EventRecord]) -> list[str]:
    if not records:
        return []
    lines = ["", f"{title}："]
    for record in records[:MAX_LISTED_EVENTS]:
        lines.append(f"- {record.event_id} {record.time:%H:%M} {'、'.join(record.rules) or '無規則'}")
    if len(records) > MAX_LISTED_EVENTS:
        lines.append(f"- 還有 {len(records) - MAX_LISTED_EVENTS} 則")
    return lines


def build_daily_summary(
    day: date,
    records: list[EventRecord],
    notices: list[GuardianNotice],
    usage: Mapping[str, int],
    *,
    mode: str,
    pause_reason: str | None,
    push_used: int,
    push_quota: int,
    false_positives: Collection[str] = (),
) -> str:
    """今日摘要（SPEC 3-7、第 10 節，決策 D30）：程式組的文字，不呼叫模型。只有規則名稱和統計，不含訊息內容和網址。"""
    today = [record for record in records if record.time.date() == day]
    high = [record for record in today if record.risk_level == "high"]
    medium = [record for record in today if record.risk_level == "medium"]
    unfinished = [record for record in today if record.risk_level is None and record.outcome in UNFINISHED_OUTCOMES]
    others = len(today) - len(high) - len(medium) - len(unfinished)

    lines = [
        f"顧家貓今日摘要（{day:%m/%d}）",
        f"訊息 {usage['messages']} 則；風險事件 {len(today)} 則"
        f"（高 {len(high)}、中 {len(medium)}、查證未完成 {len(unfinished)}、其他 {others}）",
        f"群組回覆 {usage['replies']} 則，其中主動提醒 {usage['reminders']} 則；發言模式：{mode}",
        f"模型呼叫 {usage['llm_calls']} 次、{usage['tokens']} token、錯誤 {usage['llm_errors']} 次；"
        f"本月推播 {push_used}／{push_quota} 則",
    ]
    marked = sum(1 for record in today if record.event_id in false_positives)
    if marked:
        lines.append(f"守護人標記誤報 {marked} 則")
    if pause_reason:
        lines.append(f"模型暫停中：{pause_reason}")
    if notices:
        lines += ["", "Watchdog 通知："]
        lines += [f"- {notice.time:%m/%d %H:%M} {notice.message}" for notice in notices]
    lines += _event_lines("高風險", high) + _event_lines("中風險", medium) + _event_lines("查證未完成", unfinished)
    if high or medium or unfinished:
        lines += ["", "有誤報請傳「誤報 事件編號」。"]
    return "\n".join(lines)[:MAX_DAILY_SUMMARY_CHARS]
