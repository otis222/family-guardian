"""風險事件的最小紀錄（SPEC 第 7 節）：只有 RECORD_FIELDS，不含訊息內容、網址、原始 ID 或 replyToken。保留 30 天。

檔案放在 records/（已列在 .gitignore）：
- events.jsonl：每行一筆，同一個事件後面的蓋前面的。
- false_positives.jsonl：守護人標記的誤報，只有事件編號和時間。
- state.json：發言模式和暫停狀態。不存就會在重新啟動時悄悄恢復，守護人暫停過也一樣。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .fileio import write_text_atomic
from .models import EventRecord, SchemaError

EVENTS_FILE = "events.jsonl"
FALSE_POSITIVES_FILE = "false_positives.jsonl"
STATE_FILE = "state.json"


def _read_lines(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue  # 寫到一半就關機的那一行不影響其他紀錄
        if isinstance(data, dict):
            yield data


def _lines(items: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in items)


class RecordStore:
    def __init__(self, directory: str | Path, retention_days: int) -> None:
        self.directory = Path(directory)
        self.retention = timedelta(days=retention_days)
        self._written: dict[str, dict[str, Any]] = {}

    def _path(self, name: str) -> Path:
        return self.directory / name

    def load(self, now: datetime) -> tuple[list[EventRecord], set[str]]:
        """讀回保留期限內的紀錄，順便把過期和重複的從檔案清掉。"""
        cutoff = now - self.retention
        records: dict[str, EventRecord] = {}
        for data in _read_lines(self._path(EVENTS_FILE)):
            try:
                record = EventRecord.from_dict(data)
            except SchemaError:
                continue
            if record.time >= cutoff:
                records[record.event_id] = record
        marked = {str(data.get("event_id")) for data in _read_lines(self._path(FALSE_POSITIVES_FILE))} & set(records)
        self.rewrite(records.values(), marked)
        return list(records.values()), marked

    def rewrite(self, records: Iterable[EventRecord], false_positives: Iterable[str]) -> None:
        records = list(records)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._written = {record.event_id: record.to_dict() for record in records}
        write_text_atomic(self._path(EVENTS_FILE), _lines(self._written.values()))
        write_text_atomic(self._path(FALSE_POSITIVES_FILE),
                          _lines({"event_id": event_id} for event_id in false_positives))

    def save(self, records: Iterable[EventRecord]) -> None:
        """只補寫有變動的紀錄。"""
        changed = [record.to_dict() for record in records if self._written.get(record.event_id) != record.to_dict()]
        if not changed:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._path(EVENTS_FILE).open("a", encoding="utf-8") as handle:
            handle.write(_lines(changed))
        self._written.update({data["event_id"]: data for data in changed})

    def add_false_positive(self, event_id: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._path(FALSE_POSITIVES_FILE).open("a", encoding="utf-8") as handle:
            handle.write(_lines([{"event_id": event_id}]))

    def load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path(STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_state(self, state: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self._path(STATE_FILE), json.dumps(state, ensure_ascii=False, sort_keys=True))
