"""查證 Worker（SPEC 3-4，決策 D9、D13、D14、D17）。

- 165 名單命中：程式直接判 high，不呼叫模型。
- lookup_165_sites、extract_patterns 是本機比對，程式先跑好放進第一輪 prompt。
- search_165_rumors 由模型決定要不要查：第一輪可以要求，第二輪一定要下結論，所以每個事件最多 2 次模型呼叫。
  沒有闢謠資料時不提供這個工具，1 次就結束。
- 程式的結果優先：訊息夾帶給 AI 的指令時，模型判 low 也會升到 medium。
- 失敗不重試：回傳 result=None，交給引擎記錄和處理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..data_165 import DomainList, RumorIndex
from ..llm import LLMClient, LLMError, call_llm, extract_json
from ..models import RiskSignal, SchemaError, Trigger, VerifyResult
from ..prompts import VERIFIER_SYSTEM, verifier_prompt
from ..rules import BLOCKLIST_REASON, INJECTION_REASON, RuleSet, prepare_text

SEARCH_TOOL = "search_165_rumors"
MAX_QUERY_CHARS = 100
DEFAULT_ADVICE = ["先不要點連結、不要轉帳，也不要給驗證碼", "打 165 查證，或先問家人"]


@dataclass
class VerifyOutcome:
    result: VerifyResult | None
    calls: int = 0  # 這次查證的模型呼叫次數
    tools: list[str] = field(default_factory=list)
    error: str | None = None  # 只放例外類別名稱：例外訊息可能帶有模型輸出


class Verifier:
    def __init__(self, rules: RuleSet, blocklist: DomainList, rumors: RumorIndex, client: LLMClient) -> None:
        self.rules = rules
        self.blocklist = blocklist
        self.rumors = rumors
        self.client = client

    # -------------------------------------------------------------- 工具

    def lookup_165_sites(self, trigger: Trigger) -> dict[str, Any]:
        hits = {source for url in trigger.urls if (source := self.blocklist.match(url))}
        return {"checked": len(trigger.urls), "hits": sorted(hits)}

    def extract_patterns(self, trigger: Trigger) -> dict[str, Any]:
        scores = self.rules.category_scores(prepare_text(trigger.text))
        return {"categories": [category.name for category, _ in scores]}

    def search_165_rumors(self, query: str) -> dict[str, Any]:
        return {"results": self.rumors.search(query[:MAX_QUERY_CHARS])}

    # -------------------------------------------------------------- 查證

    def verify(self, trigger: Trigger, signal: RiskSignal, *, flagged: bool = False) -> VerifyOutcome:
        lookup = self.lookup_165_sites(trigger)
        used = ["lookup_165_sites"]
        if lookup["hits"]:
            evidence = [f"165 名單來源：{source}" for source in lookup["hits"]]
            reasons = [BLOCKLIST_REASON, *(reason for reason in signal.reasons if reason != BLOCKLIST_REASON)]
            return VerifyOutcome(VerifyResult("high", reasons, evidence, list(DEFAULT_ADVICE)), tools=used)

        results: dict[str, Any] = {"lookup_165_sites": lookup, "extract_patterns": self.extract_patterns(trigger)}
        used.append("extract_patterns")
        tools = [SEARCH_TOOL] if len(self.rumors) else []
        calls = 0
        try:
            while True:
                calls += 1
                prompt = verifier_prompt(trigger, signal, results, tools)
                data = extract_json(call_llm("verifier", VERIFIER_SYSTEM, prompt, client=self.client))
                if tools and isinstance(data, dict) and "tool_calls" in data:
                    results[SEARCH_TOOL] = self.search_165_rumors(self._query(data["tool_calls"], trigger))
                    used.append(SEARCH_TOOL)
                    tools = []  # 第二輪一定要下結論；再要工具就不符合 schema，算失敗
                    continue
                return VerifyOutcome(self._apply_floor(VerifyResult.from_dict(data), flagged), calls, used)
        except (LLMError, SchemaError) as exc:
            return VerifyOutcome(None, calls, used, type(exc).__name__)

    @staticmethod
    def _query(requests: Any, trigger: Trigger) -> str:
        """模型給的關鍵詞；沒給或格式不對就用訊息本身。查詢只在本機進行。"""
        for request in requests if isinstance(requests, list) else []:
            if isinstance(request, dict) and request.get("name") == SEARCH_TOOL:
                query = request.get("query")
                if isinstance(query, str) and query.strip():
                    return query
        return trigger.text

    @staticmethod
    def _apply_floor(result: VerifyResult, flagged: bool) -> VerifyResult:
        if not flagged:
            return result
        level = "medium" if result.risk_level == "low" else result.risk_level
        return VerifyResult(level, [*result.reasons, INJECTION_REASON], result.evidence, result.advice)
