"""模擬劇本（SPEC 第 9 節）：讀取 scenarios/*.yaml 並檢查格式。

劇本裡的標註答案（label、technique）只給評估用；轉成引擎事件時一律拿掉，不會進到 Trigger。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import yaml

LABELS = ("normal", "scam", "injection")
SCENARIO_KINDS = ("text", "member_joined")  # 圖片訊息在 Phase 4 加入
MESSAGE_FIELDS = {"id", "time", "sender", "kind", "text", "mention", "label", "technique"}
USER_ID_PREFIX = "scenario:"  # 劇本角色轉成「原始 ID」時的前綴；進引擎後立刻被雜湊


class ScenarioError(ValueError):
    """劇本格式不正確。"""


@dataclass(frozen=True)
class Member:
    id: str
    name: str


@dataclass(frozen=True)
class ScenarioMessage:
    id: str
    time: datetime
    sender: str
    kind: str
    text: str
    mention: bool
    label: str
    technique: str | None = None

    def to_raw_event(self, group_id: str) -> dict[str, Any]:
        """轉成引擎的原始事件格式（和 LINE 伺服器解析 webhook 後的格式相同）。不含標註答案。"""
        return {
            "id": self.id,
            "time": self.time,
            "source": "group",
            "group_id": group_id,
            "user_id": USER_ID_PREFIX + self.sender,
            "kind": self.kind,
            "text": self.text,
            "mention": self.mention,
        }


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    group_id: str
    members: tuple[Member, ...]
    messages: tuple[ScenarioMessage, ...]

    def member_name(self, member_id: str) -> str:
        for member in self.members:
            if member.id == member_id:
                return member.name
        return member_id

    def raw_events(self) -> Iterator[dict[str, Any]]:
        for message in self.messages:
            yield message.to_raw_event(self.group_id)

    def label_counts(self) -> dict[str, int]:
        counts = Counter(message.label for message in self.messages)
        return {label: counts[label] for label in LABELS if counts[label]}


def _text(value: Any, where: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioError(f"{where}：{name} 必須是非空字串")
    return value.strip()


def _parse_time(value: Any, where: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ScenarioError(f"{where}：time 不是 ISO 8601 格式：{value!r}") from None
    else:
        raise ScenarioError(f"{where}：time 必須是時間字串，例如 \"2026-11-08T08:00:00+08:00\"")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScenarioError(f"{where}：time 必須帶時區，例如 \"2026-11-08T08:00:00+08:00\"")
    return parsed


def _parse_message(raw: Any, index: int, member_ids: set[str], file_name: str) -> ScenarioMessage:
    where = f"{file_name} 第 {index} 則訊息"
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where} 必須是對應表")
    message_id = _text(raw.get("id"), where, "id")
    where = f"{file_name} 的 {message_id}"
    unknown = set(raw) - MESSAGE_FIELDS
    if unknown:
        raise ScenarioError(f"{where}：不認得的欄位 {'、'.join(sorted(unknown))}")

    sender = _text(raw.get("sender"), where, "sender")
    if sender not in member_ids:
        raise ScenarioError(f"{where}：sender {sender!r} 不在 members 裡")
    kind = raw.get("kind", "text")
    if kind not in SCENARIO_KINDS:
        raise ScenarioError(f"{where}：kind 必須是 {'、'.join(SCENARIO_KINDS)} 其中之一，收到：{kind!r}")

    text = raw.get("text", "")
    if kind == "text":
        text = _text(text, where, "text")
    elif text not in ("", None):
        raise ScenarioError(f"{where}：{kind} 事件不能有 text")
    else:
        text = ""

    mention = raw.get("mention", False)
    if not isinstance(mention, bool):
        raise ScenarioError(f"{where}：mention 必須是 true 或 false")
    if mention and kind != "text":
        raise ScenarioError(f"{where}：只有文字訊息可以提及顧家貓")

    label = raw.get("label")
    if label not in LABELS:
        raise ScenarioError(f"{where}：label 必須是 {'、'.join(LABELS)} 其中之一，收到：{label!r}")
    technique = raw.get("technique")
    if technique is not None:
        technique = _text(technique, where, "technique")

    return ScenarioMessage(
        id=message_id,
        time=_parse_time(raw.get("time"), where),
        sender=sender,
        kind=kind,
        text=text,
        mention=mention,
        label=label,
        technique=technique,
    )


def load_scenario(path: str | Path) -> Scenario:
    scenario_path = Path(path)
    file_name = scenario_path.name
    try:
        raw = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScenarioError(f"找不到劇本：{scenario_path}") from None
    except yaml.YAMLError as exc:
        raise ScenarioError(f"劇本不是合法的 YAML：{file_name}（{exc}）") from None
    if not isinstance(raw, dict):
        raise ScenarioError(f"{file_name}：劇本最上層必須是對應表")

    name = _text(raw.get("name"), file_name, "name")
    group_id = _text(raw.get("group_id"), file_name, "group_id")
    description = raw.get("description") or ""
    if not isinstance(description, str):
        raise ScenarioError(f"{file_name}：description 必須是字串")

    members_raw = raw.get("members")
    if not isinstance(members_raw, list) or not members_raw:
        raise ScenarioError(f"{file_name}：members 必須是非空清單")
    members: list[Member] = []
    for item in members_raw:
        if not isinstance(item, dict):
            raise ScenarioError(f"{file_name}：members 的每一項都要有 id 和 name")
        members.append(Member(id=_text(item.get("id"), file_name, "member id"),
                              name=_text(item.get("name"), file_name, "member name")))
    member_ids = [member.id for member in members]
    if len(set(member_ids)) != len(member_ids):
        raise ScenarioError(f"{file_name}：members 的 id 重複")

    messages_raw = raw.get("messages")
    if not isinstance(messages_raw, list) or not messages_raw:
        raise ScenarioError(f"{file_name}：messages 必須是非空清單")
    messages = [
        _parse_message(item, index, set(member_ids), file_name)
        for index, item in enumerate(messages_raw, start=1)
    ]

    seen: set[str] = set()
    for previous, message in zip([None, *messages], messages):
        if message.id in seen:
            raise ScenarioError(f"{file_name}：訊息 id {message.id} 重複")
        seen.add(message.id)
        if previous is not None and message.time < previous.time:
            raise ScenarioError(f"{file_name}：{message.id} 的時間早於前一則 {previous.id}，訊息要依時間排序")

    return Scenario(
        name=name,
        description=description.strip(),
        group_id=group_id,
        members=tuple(members),
        messages=tuple(messages),
    )
