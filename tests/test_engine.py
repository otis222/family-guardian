"""引擎：Phase 1 驗收（offline 跑完劇本），Phase 2 起用正式的遮罩、網址抽取和快速過濾。"""

import json

import pytest

from guardian.config import PROJECT_ROOT, load_config
from guardian.data_165 import DomainList
from guardian.engine import DEMO_SALT, Engine, hash_sender
from guardian.llm import LLMClient
from guardian.models import RECORD_FIELDS, RiskSignal, SchemaError
from guardian.rules import BLOCKLIST_REASON, FastFilter, RuleSet
from guardian.scenario import load_scenario

DEMO = PROJECT_ROOT / "scenarios" / "demo_family.yaml"


@pytest.fixture
def config():
    return load_config()  # conftest 設成 offline


@pytest.fixture
def scenario():
    return load_scenario(DEMO)


def make_engine(config, **kwargs):
    return Engine(config, salt=DEMO_SALT, llm_client=LLMClient.from_config(config), **kwargs)


def raw_event(**overrides):
    event = {"id": "x1", "time": "2026-11-08T08:00:00+08:00", "source": "group", "group_id": "g",
             "user_id": "u1", "kind": "text", "text": "早安"}
    event.update(overrides)
    return event


def fast_path_entries(engine):
    return {entry.event_id: entry for entry in engine.trace if entry.stage == "fast_path" and "band" in entry.data}


# ---------------------------------------------------------------- 劇本端到端

def test_offline_demo_runs_end_to_end(config, scenario):
    assert config.llm_mode == "offline"
    engine = make_engine(config)
    summary = engine.run(scenario.raw_events())
    assert summary.events == len(scenario.messages) == 21
    assert summary.by_kind == {"text": 19, "member_joined": 1, "mention": 1}
    assert summary.by_band == {"ignore": 14, "watch": 1, "candidate": 5}
    assert [t.id for t in engine.candidate_queue] == ["m03", "m06", "m08", "m09", "m16"]
    assert {r.event_id: r.outcome for r in engine.records} == {
        "m03": "candidate", "m06": "candidate", "m08": "candidate", "m09": "candidate",
        "m14": "watch", "m16": "candidate",
    }
    assert summary.llm["total_calls"] == 0  # 快速過濾不呼叫模型（測試清單第 4 項的前半）


def test_demo_bands_match_labels(config, scenario):
    """日常訊息（包括 ATM、驗證碼、正常網址這類容易誤判的）都不處理；詐騙訊息都會被留意。"""
    engine = make_engine(config)
    engine.run(scenario.raw_events())
    bands = {event_id: entry.data["band"] for event_id, entry in fast_path_entries(engine).items()}
    for message in scenario.messages:
        if message.kind != "text":
            continue
        if message.label == "normal":
            assert bands[message.id] == "ignore", message.id
        if message.label == "scam":
            assert bands[message.id] in ("watch", "candidate"), message.id
    assert bands["m08"] == "candidate"  # 夾帶注入攻擊的詐騙訊息照樣進候選


def test_new_member_link_raises_the_score(config, scenario):
    engine = make_engine(config)
    engine.run(scenario.raw_events())
    assert "新成員剛入群就發連結" in fast_path_entries(engine)["m06"].data["reasons"]


# ---------------------------------------------------------------- 隱私

def demo_filters():
    return [pytest.param(None, id="正式規則"),
            pytest.param(lambda t: RiskSignal(100, ["測試規則"]), id="全部候選")]


@pytest.mark.parametrize("fast_filter", demo_filters())
def test_records_and_trace_never_contain_message_content(config, scenario, fast_filter):
    engine = make_engine(config, fast_filter=fast_filter)
    events = list(scenario.raw_events())
    for index, event in enumerate(events):
        event["reply_token"] = f"reply-token-{index:04d}"
    engine.run(events)
    assert engine.records

    dump = json.dumps(
        [r.to_dict() for r in engine.records]
        + [{"stage": e.stage, "message": e.message, "data": e.data} for e in engine.trace],
        ensure_ascii=False,
    )
    for message in scenario.messages:
        for start in range(max(1, len(message.text) - 7)):
            window = message.text[start:start + 8]
            if len(window) == 8:
                assert window not in dump, f"{message.id} 的內容出現在紀錄裡：{window}"
    assert "scenario:" not in dump  # 原始 ID 沒有外流
    assert "reply-token-" not in dump
    assert ".example" not in dump  # 網址也不寫進紀錄
    assert all(tuple(r.to_dict()) == RECORD_FIELDS for r in engine.records)


def test_normalize_masks_text_and_strips_url_query(config):
    text = "電話 0912-345-678，網址 https://Bad.Example/form?id=A123456789"
    trigger = make_engine(config).process(raw_event(text=text))
    assert "345-678" not in trigger.text and "＜電話＞" in trigger.text
    assert "A123456789" not in trigger.text
    assert trigger.urls == ["https://bad.example/form"]


def test_triggers_carry_no_raw_ids_or_answers(config, scenario):
    engine = make_engine(config)
    triggers = [engine.process(event) for event in scenario.raw_events()]
    for trigger in triggers:
        assert not hasattr(trigger, "label") and not hasattr(trigger, "technique")
        assert "scenario:" not in json.dumps(trigger.to_dict(), ensure_ascii=False)
    assert [t.id for t in triggers if t.kind == "mention"] == ["m09"]


def test_sender_hash_is_salted_and_stable():
    first = hash_sender("scenario:grandma", "salt-1")
    assert first == hash_sender("scenario:grandma", "salt-1")
    assert first != hash_sender("scenario:grandma", "salt-2")
    assert first != hash_sender("scenario:dad", "salt-1")
    assert first.startswith("u_") and "grandma" not in first


def test_reply_token_reaches_trigger_in_memory(config):
    trigger = make_engine(config).process(raw_event(reply_token="tok-123"))
    assert trigger.reply_token == "tok-123"


# ---------------------------------------------------------------- 元件替換與錯誤

def test_fast_filter_bands_route_events(config, scenario):
    scores = {"m03": 80, "m06": 45}

    def fake_filter(trigger):
        return RiskSignal(scores.get(trigger.id, 0), ["測試規則"] if trigger.id in scores else [])

    engine = make_engine(config, fast_filter=fake_filter)
    summary = engine.run(scenario.raw_events())
    assert summary.by_band == {"ignore": 18, "watch": 1, "candidate": 1}
    assert [t.id for t in engine.candidate_queue] == ["m03"]
    assert {r.event_id: r.outcome for r in engine.records} == {"m03": "candidate", "m06": "watch"}


def test_blocklist_hit_goes_to_candidates(config):
    fast_filter = FastFilter(RuleSet.load(), DomainList({"bad.example": "fake_investment"}))
    engine = make_engine(config, fast_filter=fast_filter)
    engine.process(raw_event(text="大家看看 https://login.bad.example/x"))
    assert [t.id for t in engine.candidate_queue] == ["x1"]
    assert engine.records[0].rules[0] == BLOCKLIST_REASON


def test_custom_redact_hook_is_used(config):
    engine = make_engine(config, redact=lambda text: text.replace("早安", "＜招呼＞"))
    assert engine.process(raw_event()).text == "＜招呼＞"


def test_engine_requires_salt(config):
    with pytest.raises(ValueError):
        Engine(config, salt="")


@pytest.mark.parametrize("overrides", [
    {"id": None},
    {"kind": "sticker"},
    {"kind": "member_joined", "mention": True, "text": ""},
    {"time": "2026-11-08T08:00:00"},  # 沒有時區
    {"time": "昨天早上"},
    {"source": "room"},
    {"user_id": ""},
])
def test_invalid_events_are_rejected(config, overrides):
    with pytest.raises(SchemaError):
        make_engine(config).process(raw_event(**overrides))
