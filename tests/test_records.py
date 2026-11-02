"""最小紀錄的檔案（SPEC 第 7 節）和引擎給真實伺服器用的介面。"""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import BASE_TIME, Scripted, event, make_engine, record_of, route_all, verify_json

from guardian import fileio
from guardian.config import load_config
from guardian.models import RECORD_FIELDS, EventRecord, GuardianReport, SchemaError
from guardian.records import RecordStore
from guardian.workers.reporter import format_report


def record(event_id="e1", days_ago=0, outcome="reported", level="high"):
    return EventRecord(event_id, BASE_TIME - timedelta(days=days_ago), "u_abc", ["要求轉帳"], level, outcome)


@pytest.fixture
def store(tmp_path):
    return RecordStore(tmp_path / "records", 30)


@pytest.fixture
def config():
    return load_config()


# ---------------------------------------------------------------- 檔案

def test_saved_records_come_back(store):
    store.save([record()])
    records, marked = RecordStore(store.directory, 30).load(BASE_TIME)
    assert [item.to_dict() for item in records] == [record().to_dict()]
    assert marked == set()


def test_only_the_minimal_fields_are_written(store):
    store.save([record()])
    line = (store.directory / "events.jsonl").read_text(encoding="utf-8").strip()
    assert set(json.loads(line)) == set(RECORD_FIELDS)


def test_unchanged_records_are_not_written_again_and_changes_win(store):
    item = record(outcome="candidate", level=None)
    store.save([item])
    store.save([item])
    path = store.directory / "events.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    item.outcome, item.risk_level = "reported", "high"
    store.save([item])
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    records, _ = RecordStore(store.directory, 30).load(BASE_TIME)
    assert len(records) == 1 and records[0].outcome == "reported"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1  # 讀回來時順便整理


def test_expired_records_and_their_marks_are_purged_on_load(store):
    store.save([record("old", days_ago=31), record("new", days_ago=29)])
    store.add_false_positive("old")
    store.add_false_positive("new")
    records, marked = RecordStore(store.directory, 30).load(BASE_TIME)
    assert [item.event_id for item in records] == ["new"] and marked == {"new"}
    assert "old" not in (store.directory / "events.jsonl").read_text(encoding="utf-8")
    assert "old" not in (store.directory / "false_positives.jsonl").read_text(encoding="utf-8")


def test_a_half_written_line_does_not_lose_everything(store):
    store.save([record("good")])
    with (store.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"event_id": "cut-off", "ti\n[1, 2]\n{"event_id": "bad"}\n')
    records, _ = RecordStore(store.directory, 30).load(BASE_TIME)
    assert [item.event_id for item in records] == ["good"]


def test_loading_with_no_files_yet_is_fine(store):
    assert store.load(BASE_TIME) == ([], set())
    assert store.load_state() == {}


def test_state_round_trip_and_damage(store):
    store.save_state({"speaking_mode": "gentle", "paused": True})
    assert store.load_state() == {"speaking_mode": "gentle", "paused": True}
    (store.directory / "state.json").write_text("not json", encoding="utf-8")
    assert store.load_state() == {}
    (store.directory / "state.json").write_text("[1]", encoding="utf-8")
    assert store.load_state() == {}


def test_replacing_a_file_tolerates_a_busy_windows_for_a_moment(tmp_path, monkeypatch):
    # 防毒軟體掃描剛寫好的檔案時，換名會暫時失敗（這台 Windows 上真的遇到過）
    real_replace = Path.replace
    failures = [PermissionError("存取被拒"), PermissionError("存取被拒")]

    def flaky(self, target):
        if failures:
            raise failures.pop()
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    monkeypatch.setattr(fileio, "REPLACE_WAIT_SECONDS", 0)
    fileio.write_text_atomic(tmp_path / "a.json", "內容")
    assert (tmp_path / "a.json").read_text(encoding="utf-8") == "內容"
    assert failures == []


def test_replacing_a_file_gives_up_after_a_few_tries(tmp_path, monkeypatch):
    attempts = []

    def always_busy(self, target):
        attempts.append(1)
        raise PermissionError("存取被拒")

    monkeypatch.setattr(Path, "replace", always_busy)
    monkeypatch.setattr(fileio, "REPLACE_WAIT_SECONDS", 0)
    with pytest.raises(PermissionError):
        fileio.write_text_atomic(tmp_path / "a.json", "內容")
    assert len(attempts) == fileio.REPLACE_ATTEMPTS


def test_record_from_dict_rejects_bad_data():
    with pytest.raises(SchemaError):
        EventRecord.from_dict({"event_id": "x"})
    with pytest.raises(SchemaError):
        EventRecord.from_dict({**record().to_dict(), "time": "昨天"})


# ---------------------------------------------------------------- 報告的文字

def test_report_text_is_complete_and_says_how_to_mark_a_false_alarm():
    report = GuardianReport("1108-103502", BASE_TIME, "u_abc", "high", ["要求轉帳"], ["有多個特徵"], [],
                            ["打 165"], "摘要文字", push=True)
    text = format_report(report)
    assert "風險高" in text and "要求轉帳" in text and "摘要文字" in text and "打 165" in text
    assert "誤報 1108-103502" in text and "u_abc" not in text


def test_report_text_for_an_unfinished_check():
    text = format_report(GuardianReport("x", BASE_TIME, "u", None, [], [], [], [], "查證沒有完成"))
    assert "查證未完成" in text and "規則：無" in text and "理由" not in text


# ---------------------------------------------------------------- 引擎給伺服器用的介面

def test_process_batch_merges_candidates_into_one_router_call(config):
    llm = Scripted(router=route_all("ignore"))
    engine = make_engine(config, llm, scores={"a": 80, "b": 80, "c": 80})
    engine.process_batch([event("a", 0), event("b", 1), event("chat", 2), event("c", 3)])
    assert llm.batches() == [["a", "b", "c"]]


def test_process_batch_skips_malformed_events(config):
    llm = Scripted(router=route_all("ignore"))
    engine = make_engine(config, llm, scores={"a": 80})
    engine.process_batch([{"id": "bad", "kind": "video"}, event("a", 0)])
    assert engine.counts["errors:schema"] == 1 and llm.batches() == [["a"]]


def test_process_batch_with_nothing_valid_does_nothing(config):
    llm = Scripted()
    make_engine(config, llm).process_batch([])
    assert llm.calls == []


def test_router_is_released_even_if_something_goes_wrong(config):
    engine = make_engine(config, Scripted(), scores={"a": 80})

    def boom(raw):
        raise RuntimeError("意外")

    engine.process = boom
    with pytest.raises(RuntimeError):
        engine.process_batch([event("a", 0)])
    assert engine._hold_router is False


def test_tick_sends_candidates_when_the_router_is_free(config):
    llm = Scripted(router=route_all("ignore"))
    engine = make_engine(config, llm, scores={"a": 80, "b": 80})
    engine.process(event("a", 0))
    engine.process(event("b", 5))  # Router 還在忙（模擬時間）
    assert llm.batches() == [["a"]]
    engine.tick(BASE_TIME + timedelta(seconds=10))
    assert llm.batches() == [["a"]]
    engine.tick(BASE_TIME + timedelta(seconds=30))
    assert llm.batches() == [["a"], ["b"]]


def test_tick_expires_threads(config):
    engine = make_engine(config, Scripted(verifier=verify_json("low")), scores={"w": 45})
    engine.process(event("w", 0))
    assert len(engine.threads) == 1
    engine.tick(BASE_TIME + timedelta(days=2))
    assert len(engine.threads) == 0


def test_watchdog_checks_on_ticks_and_rolls_the_day(config):
    engine = make_engine(config, Scripted(), scores={})
    engine.tick(BASE_TIME)
    assert engine.watchdog.checks == 0
    engine.tick(BASE_TIME + timedelta(minutes=config.watchdog.check_every_minutes))
    assert engine.watchdog.checks == 1
    engine.llm.paused = True
    engine.tick(BASE_TIME + timedelta(days=1))
    assert engine.llm.paused is False


def test_false_positive_needs_a_known_event(config):
    engine = make_engine(config, Scripted(router=route_all("ignore")), scores={"a": 80})
    engine.process(event("a", 0))
    assert engine.mark_false_positive("a") is True
    assert engine.mark_false_positive("nope") is False
    assert engine.false_positives == {"a"}


def test_restore_and_forget(config):
    engine = make_engine(config, Scripted())
    engine.restore_records([record("old", days_ago=40), record("new")], {"old", "new", "gone"})
    assert engine.risk_level_of("new") == "high" and engine.false_positives == {"old", "new"}
    engine.forget_before(BASE_TIME - timedelta(days=30))
    assert record_of(engine, "old") is None and engine.false_positives == {"new"}
    assert engine.risk_level_of("old") is None
    assert engine.mark_false_positive("old") is False


def test_trim_memory_keeps_the_latest_trace_and_drops_model_text(config):
    engine = make_engine(config, Scripted(router=route_all("ignore")), scores={"a": 80})
    engine.process(event("a", 0))
    assert engine.decisions
    engine.trim_memory(keep_trace=2)
    assert len(engine.trace) == 2 and engine.decisions == []


def test_claim_push_counts_recipients_and_refuses_when_there_is_not_enough(config):
    engine = make_engine(config, Scripted())
    watchdog = engine.watchdog
    quota = config.watchdog.push_quota_monthly
    assert watchdog.claim_push("high", BASE_TIME, count=quota - 1)
    assert not watchdog.claim_push("high", BASE_TIME, count=2)  # 只剩 1 則，推給 2 個人不夠
    assert watchdog.claim_push("high", BASE_TIME, count=1)
    assert watchdog.push_used == quota


def test_sync_push_usage_never_goes_backwards(config):
    watchdog = make_engine(config, Scripted()).watchdog
    watchdog.sync_push_usage(10)
    watchdog.sync_push_usage(5)
    assert watchdog.push_used == 10
