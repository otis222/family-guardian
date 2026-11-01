"""Router（SPEC 3-3、第 4 節）：批次分流、發言模式、發言前自檢。

顧家貓對外的每一句話都從這裡出去。合格與否以程式檢查為準：
初稿不合格就請模型改寫一次，仍不合格、或模型失敗，就用固定模板。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .llm import LLMClient, LLMError, call_llm, extract_json
from .models import Decision, GroupReply, RiskSignal, Trigger, parse_decisions
from .prompts import ROUTER_SYSTEM, SPEAKER_SYSTEM, draft_prompt, rewrite_prompt, router_prompt
from .urls import extract_urls

REPLY_TEMPLATE = "這則訊息有詐騙常見的特徵。請先不要點連結、不要轉帳，也不要給驗證碼。可以打 165 查證，或先問問家人。"
NEUTRAL_TEMPLATE = "目前沒看到明顯的詐騙特徵，但還是要小心。有人要求匯款、給驗證碼或點連結時，先停一下，打 165 查證或問問家人。"
UNVERIFIED_TEMPLATE = "顧家貓現在沒辦法完成查證。請先不要點連結、不要轉帳，可以打 165 查證，或先問問家人。"

_ASSERTION = re.compile(
    r"是[^，。！？\s]{0,4}詐騙(?!常見|特徵)|這[則個封些]?詐騙|詐騙(?:集團|犯|份子|分子)"
    r"|騙子|騙局|騙人|是假(?:的|消息|訊息)"
)
_NAMING = re.compile(
    r"[@＠]|阿嬤|阿公|奶奶|爺爺|外婆|外公|爸爸|媽媽|大伯|伯伯|叔叔|姑姑|阿姨|舅舅"
    r"|[表堂](?:哥|姊|姐|弟|妹)|哥哥|姊姊|姐姐|弟弟|妹妹|兒子|女兒|孫子|孫女"
)
_BLAME = re.compile(
    r"[你妳您](?:怎麼|不該|不應該|竟然|居然|又|為什麼)|都是[你妳您]|亂傳|亂轉|害大家|太傻|太笨|愚蠢|粗心"
    r"|怎麼會(?:相信|信)|不要再(?:轉傳|亂傳)"
)


def review_draft(text: str, max_chars: int) -> list[str]:
    """發言前自檢：回傳問題類別。空的代表合格。除了規格的三項，也擋沒給 165 和貼網址（決策 D22）。"""
    checks = (
        ("斷言", _ASSERTION.search(text)),
        ("點名", _NAMING.search(text)),
        ("責怪", _BLAME.search(text)),
        ("超過字數", len(text) > max_chars),
        ("沒有 165", "165" not in text),
        ("含網址", extract_urls(text)),
    )
    return [name for name, failed in checks if failed]


def should_speak(mode: str, level: str | None, is_mention: bool) -> bool:
    """SPEC 第 4 節：不論哪個模式，被 @ 都要回應；其他情況只有 gentle 模式的 high 會發言。"""
    return is_mention or (mode == "gentle" and level == "high")


@dataclass
class Candidate:
    trigger: Trigger
    signal: RiskSignal
    flagged: bool = False  # 夾帶給 AI 的指令：模型不能放行（決策 D17）


class Router:
    def __init__(self, client: LLMClient, max_reply_chars: int = 100) -> None:
        self.client = client
        self.max_reply_chars = max_reply_chars

    def decide(self, candidates: Iterable[Candidate]) -> list[tuple[Candidate, Decision, bool]]:
        """一次呼叫處理整批候選。回傳 (候選, 決策, 是否被程式覆蓋)。呼叫或格式失敗時丟出例外，由引擎處理。"""
        candidates = list(candidates)
        prompt = router_prompt((item.trigger, item.signal) for item in candidates)
        decisions: dict[str, Decision] = {}
        for decision in parse_decisions(extract_json(call_llm("router", ROUTER_SYSTEM, prompt, client=self.client))):
            decisions.setdefault(decision.event_id, decision)  # 多出來的事件不會被取用

        results = []
        for item in candidates:
            event_id = item.trigger.id
            decision = decisions.get(event_id) or Decision(event_id, "watch", "模型漏掉這個事件，程式當成 watch")
            overridden = item.flagged and decision.action != "verify"
            if overridden:
                decision = Decision(event_id, "verify", "訊息夾帶給 AI 的指令，程式要求查證")
            results.append((item, decision, overridden))
        return results

    def compose_reply(self, event_id: str, level: str, rules: list[str]) -> tuple[GroupReply, list[str]]:
        """初稿 → 自檢 → 改寫一次 → 模板。回傳回覆和給決策紀錄的說明（只有問題類別，沒有內容）。"""
        limit = self.max_reply_chars
        notes: list[str] = []
        try:
            draft = call_llm("router", SPEAKER_SYSTEM, draft_prompt(level, rules, limit), client=self.client).strip()
            problems = review_draft(draft, limit)
            if not problems:
                notes.append("初稿合格")
                return GroupReply(event_id, draft, "model"), notes
            notes.append(f"初稿不合格：{'、'.join(problems)}")
            prompt = rewrite_prompt(draft, problems, level, rules, limit)
            rewrite = call_llm("router", SPEAKER_SYSTEM, prompt, client=self.client).strip()
            problems = review_draft(rewrite, limit)
            if not problems:
                notes.append("改寫後合格")
                return GroupReply(event_id, rewrite, "rewrite"), notes
            notes.append(f"改寫仍不合格：{'、'.join(problems)}，改用模板")
        except LLMError as exc:
            notes.append(f"模型失敗（{type(exc).__name__}），改用模板，未重試")
        return GroupReply(event_id, REPLY_TEMPLATE, "template"), notes
