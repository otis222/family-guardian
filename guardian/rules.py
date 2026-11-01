"""快速過濾（SPEC 3-2，決策 D8）：純程式，不呼叫模型。

分數 ＝ 各類行為特徵的分數加總 ＋ 情境加權，上限 max_score；165 名單命中直接給 blocklist_score。
- 每條規則有自己的權重。同一類取命中規則的最高權重，命中兩條以上再加 multi_hit_bonus。
- reasons 只放類別和情境的名稱（會寫進風險事件紀錄），不放訊息原文。
權重、關鍵詞和正反例都在 rules.yaml。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .data_165 import DomainList
from .models import RiskSignal, Trigger

if TYPE_CHECKING:
    from .config import Config

DEFAULT_RULES_PATH = Path(__file__).with_name("rules.yaml")
BLOCKLIST_REASON = "165 涉詐網站名單"
INJECTION_REASON = "訊息夾帶給 AI 的指令"
_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class RulesError(ValueError):
    """rules.yaml 格式不正確。"""


def prepare_text(text: str) -> str:
    """比對前的正規化（NFKC：全形英數轉半形）。只用在比對，不改動 Trigger 的內容。"""
    return unicodedata.normalize("NFKC", text)


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    weight: int


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    rules: tuple[Rule, ...]
    positive: tuple[str, ...] = ()
    negative: tuple[str, ...] = ()

    def score(self, text: str, bonus: int) -> int:
        """text 要先經過 prepare_text。沒命中回傳 0。"""
        weights = [rule.weight for rule in self.rules if rule.pattern.search(text)]
        if not weights:
            return 0
        return max(weights) + (bonus if len(weights) >= 2 else 0)


def _compile(pattern: Any, where: str) -> re.Pattern:
    if not isinstance(pattern, str) or not pattern:
        raise RulesError(f"{where}：pattern 必須是非空字串")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise RulesError(f"{where}：正規表示式有誤（{exc}）") from None


def _weight(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise RulesError(f"{where}：weight 必須是 1 到 100 的整數")
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RulesError(f"{where} 必須是字串清單")
    return tuple(value)


def _name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RulesError(f"{where}：name 必須是非空字串")
    return value.strip()


@dataclass(frozen=True)
class RuleSet:
    categories: tuple[Category, ...]
    multi_hit_bonus: int
    max_score: int
    blocklist_score: int
    new_member_name: str
    new_member_weight: int
    new_member_hours: int
    forwarded_name: str
    forwarded_weight: int
    forwarded_markers: tuple[str, ...]
    amount_name: str
    amount_weight: int
    amount_patterns: tuple[re.Pattern, ...]
    injection_patterns: tuple[re.Pattern, ...] = ()
    injection_positive: tuple[str, ...] = ()
    injection_negative: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: str | Path = DEFAULT_RULES_PATH) -> RuleSet:
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RulesError(f"找不到規則檔：{path}") from None
        except yaml.YAMLError as exc:
            raise RulesError(f"規則檔不是合法的 YAML：{path.name}（{exc}）") from None
        if not isinstance(raw, dict):
            raise RulesError(f"{path.name}：最上層必須是對應表")

        categories: list[Category] = []
        for index, item in enumerate(raw.get("categories") or [], start=1):
            if not isinstance(item, dict):
                raise RulesError(f"第 {index} 個類別必須是對應表")
            category_id = item.get("id")
            if not isinstance(category_id, str) or not _ID.match(category_id):
                raise RulesError(f"第 {index} 個類別的 id 必須是小寫英文、數字或底線")
            if any(existing.id == category_id for existing in categories):
                raise RulesError(f"類別 id 重複：{category_id}")
            patterns = item.get("patterns")
            if not isinstance(patterns, list) or not patterns:
                raise RulesError(f"{category_id}：patterns 必須是非空清單")
            rules = []
            for number, entry in enumerate(patterns, start=1):
                where = f"{category_id} 第 {number} 條規則"
                if not isinstance(entry, dict):
                    raise RulesError(f"{where} 必須寫成 {{pattern: ..., weight: ...}}")
                rules.append(Rule(_compile(entry.get("pattern"), where), _weight(entry.get("weight"), where)))
            examples = item.get("examples") or {}
            if not isinstance(examples, dict):
                raise RulesError(f"{category_id}：examples 必須是對應表")
            categories.append(Category(
                id=category_id,
                name=_name(item.get("name"), category_id),
                rules=tuple(rules),
                positive=_strings(examples.get("positive"), f"{category_id} 的正例"),
                negative=_strings(examples.get("negative"), f"{category_id} 的反例"),
            ))
        if not categories:
            raise RulesError(f"{path.name}：至少要有一個類別")

        context = raw.get("context")
        if not isinstance(context, dict):
            raise RulesError(f"{path.name}：缺少 context")
        sections = {}
        for key in ("new_member_link", "forwarded_link", "amount"):
            section = context.get(key)
            if not isinstance(section, dict):
                raise RulesError(f"context 缺少 {key}")
            sections[key] = section
        hours = sections["new_member_link"].get("window_hours")
        if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1:
            raise RulesError("context.new_member_link.window_hours 必須是正整數")
        amount_patterns = _strings(sections["amount"].get("patterns"), "context.amount.patterns")
        if not amount_patterns:
            raise RulesError("context.amount.patterns 不能是空的")

        injection = raw.get("injection_markers")
        if not isinstance(injection, dict):
            raise RulesError(f"{path.name}：缺少 injection_markers")
        injection_patterns = _strings(injection.get("patterns"), "injection_markers.patterns")
        if not injection_patterns:
            raise RulesError("injection_markers.patterns 不能是空的")
        injection_examples = injection.get("examples") or {}

        def setting(key: str, minimum: int) -> int:
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 100:
                raise RulesError(f"{key} 必須是 {minimum} 到 100 的整數")
            return value

        return cls(
            categories=tuple(categories),
            multi_hit_bonus=setting("multi_hit_bonus", 0),
            max_score=setting("max_score", 1),
            blocklist_score=setting("blocklist_score", 1),
            new_member_name=_name(sections["new_member_link"].get("name"), "context.new_member_link"),
            new_member_weight=_weight(sections["new_member_link"].get("weight"), "context.new_member_link"),
            new_member_hours=hours,
            forwarded_name=_name(sections["forwarded_link"].get("name"), "context.forwarded_link"),
            forwarded_weight=_weight(sections["forwarded_link"].get("weight"), "context.forwarded_link"),
            forwarded_markers=_strings(sections["forwarded_link"].get("markers"), "context.forwarded_link.markers"),
            amount_name=_name(sections["amount"].get("name"), "context.amount"),
            amount_weight=_weight(sections["amount"].get("weight"), "context.amount"),
            amount_patterns=tuple(_compile(pattern, "context.amount") for pattern in amount_patterns),
            injection_patterns=tuple(_compile(pattern, "injection_markers") for pattern in injection_patterns),
            injection_positive=_strings(injection_examples.get("positive"), "injection_markers 的正例"),
            injection_negative=_strings(injection_examples.get("negative"), "injection_markers 的反例"),
        )

    def category_scores(self, text: str) -> list[tuple[Category, int]]:
        """回傳命中的類別和各自的分數。text 要先經過 prepare_text。"""
        scores = []
        for category in self.categories:
            value = category.score(text, self.multi_hit_bonus)
            if value:
                scores.append((category, value))
        return scores

    def matched_category_ids(self, text: str) -> set[str]:
        return {category.id for category, _ in self.category_scores(prepare_text(text))}

    def flags_injection(self, text: str) -> bool:
        """訊息裡有要 AI 改變行為的文字（決策 D17）。不計分，只限制模型不能放行。"""
        prepared = prepare_text(text)
        return any(pattern.search(prepared) for pattern in self.injection_patterns)


class FastFilter:
    """快速過濾：依規則、情境和 165 名單給分。會記住新成員入群的時間（只存雜湊和時間）。"""

    def __init__(self, rules: RuleSet, blocklist: DomainList | None = None) -> None:
        self.rules = rules
        self.blocklist = blocklist if blocklist is not None else DomainList()
        self._joined: dict[str, datetime] = {}

    @classmethod
    def from_config(cls, config: Config, rules_path: str | Path = DEFAULT_RULES_PATH) -> FastFilter:
        settings = config.data_165
        return cls(RuleSet.load(rules_path), DomainList.from_cache(settings.cache_dir, settings.sources))

    def observe(self, trigger: Trigger) -> None:
        """每個事件都會經過這裡；目前只記新成員入群。"""
        if trigger.kind == "member_joined" and trigger.sender_hash:
            window = timedelta(hours=self.rules.new_member_hours)
            self._joined = {key: when for key, when in self._joined.items() if trigger.time - when <= window}
            self._joined[trigger.sender_hash] = trigger.time

    def _is_new_member(self, trigger: Trigger) -> bool:
        joined = self._joined.get(trigger.sender_hash)
        if joined is None:
            return False
        return timedelta(0) <= trigger.time - joined <= timedelta(hours=self.rules.new_member_hours)

    def __call__(self, trigger: Trigger) -> RiskSignal:
        rules = self.rules
        text = prepare_text(trigger.text)
        score, reasons = 0, []
        for category, value in rules.category_scores(text):
            score += value
            reasons.append(category.name)
        if trigger.urls and self._is_new_member(trigger):
            score += rules.new_member_weight
            reasons.append(rules.new_member_name)
        if trigger.urls and any(marker in text for marker in rules.forwarded_markers):
            score += rules.forwarded_weight
            reasons.append(rules.forwarded_name)
        if any(pattern.search(text) for pattern in rules.amount_patterns):
            score += rules.amount_weight
            reasons.append(rules.amount_name)
        score = min(score, rules.max_score)
        if any(self.blocklist.match(url) for url in trigger.urls):
            score = max(score, rules.blocklist_score)
            reasons.insert(0, BLOCKLIST_REASON)
        return RiskSignal(score=score, reasons=reasons)
