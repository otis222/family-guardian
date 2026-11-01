"""回報 Worker（SPEC 3-4，決策 D19、D20）：給守護人的結構化報告。

欄位由程式組好，不含訊息內容；模型文字裡的網址換成＜網址＞，避免守護人誤點。
只有模型查證過的 high 才請模型寫一句摘要（high 才會即時推播，SPEC 第 10 節），其他都用模板，不花 token。
"""

from __future__ import annotations

from ..llm import LLMClient, LLMError, call_llm
from ..models import RISK_LABELS, GuardianReport, RiskSignal, Trigger, VerifyResult
from ..prompts import REPORTER_SYSTEM, reporter_prompt
from ..urls import strip_urls

MAX_SUMMARY_CHARS = 200


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


def build_failure_report(trigger: Trigger, signal: RiskSignal) -> GuardianReport:
    """查證失敗時也要讓守護人知道，只有規則比對結果。"""
    rules = list(signal.reasons)
    summary = f"查證沒有完成，只有規則比對結果：{'、'.join(rules) or '無'}。"
    return GuardianReport(trigger.id, trigger.time, trigger.sender_hash, None, rules, [], [], [], summary, push=False)
