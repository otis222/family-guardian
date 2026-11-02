"""實驗的三種做法和消融實驗（SPEC 11-1）。

A、B 是 Engine 的子類別，只換掉分流的部分；正規化、遮罩、回報、發言、Watchdog 和決策紀錄都共用，比較才公平。
模擬和實驗用的 165 資料都是固定的（決策 D35）：名單用劇本指定的模擬網域，闢謠專區用錄製時凍結的那一份。
"""

from __future__ import annotations

import dataclasses

from .config import Config
from .data_165 import DomainList, RumorIndex
from .engine import DEMO_SALT, Engine
from .llm import LLMClient, LLMError, call_llm, extract_json
from .models import BAND_LABELS, RISK_LABELS, GroupReply, RiskSignal, SchemaError, Trigger, VerifyResult
from .prompts import SINGLE_SYSTEM, single_prompt
from .router import NEUTRAL_TEMPLATE, REPLY_TEMPLATE, UNVERIFIED_TEMPLATE, should_speak
from .rules import FastFilter, RuleSet
from .scenario import Scenario
from .workers.reporter import build_report
from .workers.verifier import DEFAULT_ADVICE

APPROACH_LABELS = {
    "A": "A 純規則",
    "B": "B 單一 LLM",
    "C": "C Super-agent",
    "C-no-filter": "C 拿掉快速過濾",
    "C-no-early": "C 拿掉提前行動",
    "C-no-batch": "C 拿掉批次處理",
}
SIMULATOR_APPROACHES = ("C", "A", "B")
SIMULATED_SOURCE = "simulated"
BAND_LEVELS = {"candidate": "high", "watch": "medium"}


class PassThroughFilter(FastFilter):
    """不做規則比對，每則文字訊息都給同一個分數。名單和注入標記還留著，給查證 Worker 和程式防線用。"""

    passthrough = True

    def __init__(self, base: FastFilter, score: int) -> None:
        super().__init__(base.rules, base.blocklist)
        self.score = score

    def __call__(self, trigger: Trigger) -> RiskSignal:
        return RiskSignal(self.score, [])


class _TemplateReplies(Engine):
    """A、B 共用：查出風險就報告；要發言時用固定模板，不再多花模型呼叫。"""

    def _report_and_speak(self, trigger: Trigger, signal: RiskSignal, result: VerifyResult) -> None:
        level = result.risk_level
        self._add_report(trigger, build_report(trigger, signal, result, client=self.llm, used_model=False))
        speak = should_speak(self.speaking_mode, level, trigger.is_mention)
        if speak:
            reply = GroupReply(trigger.id, REPLY_TEMPLATE, "template")
            self._attach_card(trigger, reply)
            self._add_reply(trigger, reply)
        else:
            self._trace(trigger, "speak", f"{self.speaking_mode} 模式，不在群組發言", mode=self.speaking_mode)
        self._finish(trigger, signal, level, "reported_replied" if speak else "reported")


class RulesOnlyEngine(_TemplateReplies):
    """A：不呼叫模型。候選當 high，觀察區間當 medium。"""

    def _route(self, trigger: Trigger, signal: RiskSignal, band: str) -> None:
        level = BAND_LEVELS.get(band)
        if level is None:
            if trigger.is_mention:
                self._reply_template(trigger, NEUTRAL_TEMPLATE)
            return
        self._trace(trigger, "approach", f"純規則：{BAND_LABELS[band]}當成風險{RISK_LABELS[level]}", level=level)
        self._report_and_speak(trigger, signal, VerifyResult(level, list(signal.reasons), [], list(DEFAULT_ADVICE)))


class SingleLLMEngine(_TemplateReplies):
    """B：每則文字訊息都問一次模型，沒有規則結果、Router 和程式防線。訊息一樣先遮罩。"""

    def _route(self, trigger: Trigger, signal: RiskSignal, band: str) -> None:
        try:
            text = call_llm("verifier", SINGLE_SYSTEM, single_prompt(trigger), client=self.llm)
            result = VerifyResult.from_dict(extract_json(text))
        except (LLMError, SchemaError) as exc:
            self.counts["errors:verifier"] += 1
            self._trace(trigger, "approach", f"單一 LLM 失敗（{type(exc).__name__}），未重試", error=type(exc).__name__)
            self._finish(trigger, signal, None, "verify_failed")
            if trigger.is_mention:
                self._reply_template(trigger, UNVERIFIED_TEMPLATE)
            return
        level = result.risk_level
        self._trace(trigger, "approach", f"單一 LLM：風險{RISK_LABELS[level]}", level=level)
        if level == "low":
            if trigger.is_mention:
                self._reply_template(trigger, NEUTRAL_TEMPLATE)
            return
        self._report_and_speak(trigger, signal, result)


def build_engine(approach: str, config: Config, scenario: Scenario, *, llm_client: LLMClient | None = None) -> Engine:
    """模擬和實驗用的引擎。真實群組（LINE 伺服器）不走這裡，直接用 Engine 和真實的 165 資料。"""
    base = FastFilter(RuleSet.load(), DomainList(dict.fromkeys(scenario.blocklist, SIMULATED_SOURCE)))
    rumors = RumorIndex.from_cache(config.experiment.snapshot_dir, config.data_165.rumors)
    common = {"salt": DEMO_SALT, "llm_client": llm_client, "rumors": rumors}
    if approach == "A":
        return RulesOnlyEngine(config, fast_filter=base, **common)
    if approach == "B":
        return SingleLLMEngine(config, fast_filter=PassThroughFilter(base, 0), **common)
    if approach == "C":
        return Engine(config, fast_filter=base, **common)
    if approach == "C-no-filter":
        return Engine(config, fast_filter=PassThroughFilter(base, config.thresholds.candidate), **common)
    if approach == "C-no-early":
        return Engine(config, fast_filter=base, early_action=False, **common)
    if approach == "C-no-batch":
        return Engine(dataclasses.replace(config, router_latency_seconds=0), fast_filter=base, **common)
    raise ValueError(f"未知的做法：{approach!r}（可用：{'、'.join(APPROACH_LABELS)}）")
