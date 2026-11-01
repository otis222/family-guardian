"""資料結構與 schema 檢查（SPEC 3-1 到 3-5、第 7 節）。"""

from datetime import datetime, timedelta, timezone

import pytest

from guardian.models import (
    RECORD_FIELDS,
    Decision,
    EventRecord,
    RiskSignal,
    SchemaError,
    Thread,
    Trigger,
    VerifyResult,
    parse_decisions,
)

TW = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 8, 8, 0, tzinfo=TW)


def make_trigger(**overrides):
    data = dict(id="m01", time=NOW, source="group", group_id="g1", sender_hash="u_abc", kind="text", text="早安")
    data.update(overrides)
    return Trigger(**data)


# ---------------------------------------------------------------- Trigger

def test_reply_token_stays_in_memory_only():
    trigger = make_trigger(reply_token="secret-reply-token")
    assert trigger.reply_token == "secret-reply-token"  # 記憶體裡還能用
    assert "secret-reply-token" not in repr(trigger)
    assert "reply_token" not in trigger.to_dict()


@pytest.mark.parametrize("overrides", [
    {"kind": "video"},
    {"source": "room"},
    {"time": datetime(2026, 11, 8, 8, 0)},  # 沒有時區
    {"group_id": None},  # 群組事件一定要有 group_id
    {"sender_hash": ""},
    {"urls": "https://a.example"},
    {"id": ""},
])
def test_trigger_rejects_bad_fields(overrides):
    with pytest.raises(SchemaError):
        make_trigger(**overrides)


def test_mention_kind_and_flag_must_agree():
    assert make_trigger(kind="mention", is_mention=True).is_mention
    with pytest.raises(SchemaError):
        make_trigger(kind="mention", is_mention=False)
    with pytest.raises(SchemaError):
        make_trigger(kind="text", is_mention=True)


def test_join_event_has_no_sender():
    assert make_trigger(kind="join", sender_hash="", text="").sender_hash == ""


def test_private_message_needs_no_group():
    assert make_trigger(source="user", group_id=None).group_id is None


# ---------------------------------------------------------------- RiskSignal

@pytest.mark.parametrize("score, band", [
    (0, "ignore"), (29, "ignore"), (30, "watch"), (59, "watch"), (60, "candidate"), (100, "candidate"),
])
def test_risk_signal_bands(score, band):
    assert RiskSignal(score).band(30, 60) == band


@pytest.mark.parametrize("kwargs", [{"score": -1}, {"score": True}, {"score": 1.5}, {"score": 10, "reasons": "轉帳"}])
def test_risk_signal_rejects_bad_values(kwargs):
    with pytest.raises(SchemaError):
        RiskSignal(**kwargs)


def test_band_rejects_bad_thresholds():
    with pytest.raises(SchemaError):
        RiskSignal(10).band(60, 30)


# ---------------------------------------------------------------- Decision

def test_decision_from_dict():
    decision = Decision.from_dict({"event_id": "m03", "action": "verify", "reason": "有轉帳要求", "extra": 1})
    assert decision.to_dict() == {"event_id": "m03", "action": "verify", "reason": "有轉帳要求"}


@pytest.mark.parametrize("data", [
    {"event_id": "m03", "action": "alert", "reason": "x"},  # 不在 schema 裡的動作
    {"event_id": "m03", "action": "ignore"},  # 缺 reason
    {"event_id": "", "action": "ignore", "reason": "x"},
    ["m03", "ignore", "x"],
])
def test_decision_rejects_bad_output(data):
    with pytest.raises(SchemaError):
        Decision.from_dict(data)


def test_parse_decisions_accepts_batch_shapes():
    one = {"event_id": "a", "action": "ignore", "reason": "日常"}
    two = {"event_id": "b", "action": "watch", "reason": "再觀察"}
    assert [d.event_id for d in parse_decisions(one)] == ["a"]
    assert [d.event_id for d in parse_decisions([one, two])] == ["a", "b"]
    assert [d.event_id for d in parse_decisions({"decisions": [one, two]})] == ["a", "b"]
    with pytest.raises(SchemaError):
        parse_decisions("ignore")


# ---------------------------------------------------------------- VerifyResult

def test_verify_result_from_dict():
    data = {"risk_level": "high", "reasons": ["保證獲利"], "evidence": ["165 名單命中"], "advice": ["打 165"]}
    assert VerifyResult.from_dict(data).to_dict() == data


@pytest.mark.parametrize("data", [
    {"risk_level": "critical", "reasons": [], "evidence": [], "advice": []},
    {"risk_level": "low", "reasons": "不是清單", "evidence": [], "advice": []},
    {"risk_level": "low", "reasons": [], "evidence": []},
])
def test_verify_result_rejects_bad_output(data):
    with pytest.raises(SchemaError):
        VerifyResult.from_dict(data)


# ---------------------------------------------------------------- Thread

def test_thread_expires_after_ttl():
    thread = Thread.open(group_id="g1", value="u_abc", key_type="sender", now=NOW, ttl_hours=24)
    assert thread.key == ("g1", "u_abc")
    assert not thread.is_expired(NOW + timedelta(hours=23, minutes=59))
    assert thread.is_expired(NOW + timedelta(hours=24))


def test_thread_rejects_bad_key_type():
    with pytest.raises(SchemaError):
        Thread.open(group_id="g1", value="u_abc", key_type="topic", now=NOW)


# ---------------------------------------------------------------- EventRecord

def test_event_record_has_only_minimal_fields():
    record = EventRecord(event_id="m03", time=NOW, sender_hash="u_abc", rules=["保證獲利"],
                         risk_level=None, outcome="candidate")
    assert tuple(record.to_dict()) == RECORD_FIELDS


def test_event_record_rejects_unknown_risk_level():
    with pytest.raises(SchemaError):
        EventRecord(event_id="m03", time=NOW, sender_hash="u_abc", rules=[], risk_level="severe", outcome="x")
