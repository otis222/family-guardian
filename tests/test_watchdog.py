"""Watchdog 和今日摘要（SPEC 3-7、第 13 節第 10 項；Phase 4 驗收：Watchdog 觸發測試通過）。"""

import dataclasses
from datetime import timedelta

import pytest
from helpers import BASE_TIME, RULE, Scripted, event, make_engine, record_of, route_all, verify_json

from guardian.config import PROJECT_ROOT, load_config
from guardian.data_165 import DomainList
from guardian.llm import LLMClient, LLMPausedError, call_llm
from guardian.router import NEUTRAL_TEMPLATE, REPLY_TEMPLATE, UNVERIFIED_TEMPLATE
from guardian.rules import BLOCKLIST_REASON, FastFilter, RuleSet
from guardian.scenario import load_scenario

DEMO = PROJECT_ROOT / "scenarios" / "demo_family.yaml"
DAY = 24 * 3600


@pytest.fixture
def base():
    return load_config()


def tuned(config, **changes):
    """每則訊息都檢查，測試才不用湊數量。"""
    changes.setdefault("check_every_messages", 1)
    return dataclasses.replace(config, watchdog=dataclasses.replace(config.watchdog, **changes))


def kinds(engine):
    return [notice.kind for notice in engine.notices]


# ---------------------------------------------------------------- 檢查的時機

def test_checks_every_m_messages(base):
    engine = make_engine(tuned(base, check_every_messages=3), scores={})
    for index in range(7):
        engine.process(event(f"e{index}", index))
    assert engine.watchdog.checks == 2


def test_checks_every_n_minutes_on_event_time(base):
    engine = make_engine(tuned(base, check_every_messages=100, check_every_minutes=10), scores={})
    engine.process(event("a", 0))
    engine.process(event("b", 9 * 60))
    assert engine.watchdog.checks == 0
    engine.process(event("c", 10 * 60))
    assert engine.watchdog.checks == 1
    engine.process(event("d", 15 * 60))
    assert engine.watchdog.checks == 1  # 從上次檢查重新算


def test_join_events_are_not_messages(base):
    engine = make_engine(tuned(base, check_every_messages=2), scores={})
    engine.process({**event("j", 0), "kind": "member_joined", "text": ""})
    engine.process(event("a", 1))
    assert engine.watchdog.checks == 0 and engine.watchdog.usage_on(BASE_TIME.date())["messages"] == 1


# ---------------------------------------------------------------- 提醒過多：切回 silent（第 10 項）

def test_too_many_reminders_switch_back_to_silent_and_notify(base):
    llm = Scripted(verifier=verify_json("high"))
    scores = {f"c{index}": 80 for index in range(5)}
    engine = make_engine(tuned(base, max_reminders_per_100=2), llm, scores=scores)
    engine.speaking_mode = "gentle"
    engine.run(event(f"c{index}", index * 60) for index in range(5))
    assert engine.speaking_mode == "silent"
    assert [reply.event_id for reply in engine.replies] == ["c0", "c1", "c2"]  # 第 3 次超過，下一則開始安靜
    assert len(engine.reports) == 5  # 守護人照樣收到報告
    assert kinds(engine) == ["too_many_reminders"]
    assert "切回 silent" in engine.notices[0].message
    assert any(entry.stage == "watchdog" for entry in engine.trace)


def test_family_can_still_ask_after_the_switch(base):
    engine = make_engine(tuned(base, max_reminders_per_100=0), Scripted(verifier=verify_json("high")),
                         scores={"c": 80, "ask": 80})
    engine.speaking_mode = "gentle"
    engine.run([event("c", 0), event("ask", 60, mention=True)])
    assert engine.speaking_mode == "silent"
    assert [reply.event_id for reply in engine.replies] == ["c", "ask"]


def test_answers_to_mentions_do_not_count_as_reminders(base):
    scores = {f"q{index}": 80 for index in range(4)}
    engine = make_engine(tuned(base, max_reminders_per_100=1), Scripted(verifier=verify_json("high")), scores=scores)
    engine.speaking_mode = "gentle"
    engine.run(event(f"q{index}", index * 60, mention=True) for index in range(4))
    assert engine.speaking_mode == "gentle" and engine.notices == []
    assert len(engine.replies) == 4


def test_only_the_last_100_messages_count(base):
    scores = {f"c{index}": 80 for index in range(4)}
    engine = make_engine(tuned(base, max_reminders_per_100=1), Scripted(verifier=verify_json("high")), scores=scores)
    engine.speaking_mode = "gentle"
    clock = 0
    for index in range(4):
        engine.process(event(f"c{index}", clock))
        for chat in range(100):
            clock += 60
            engine.process(event(f"chat{index}-{chat}", clock))
    assert engine.speaking_mode == "gentle" and len(engine.replies) == 4


def test_guardian_switching_back_starts_a_fresh_count(base):
    scores = {f"c{index}": 80 for index in range(4)}
    engine = make_engine(tuned(base, max_reminders_per_100=1), Scripted(verifier=verify_json("high")), scores=scores)
    engine.speaking_mode = "gentle"
    for index in range(3):
        engine.process(event(f"c{index}", index * 60))
    assert engine.speaking_mode == "silent"
    engine.speaking_mode = "gentle"  # 之後由管理指令「模式 gentle」切換
    engine.process(event("c3", 180))
    assert engine.speaking_mode == "gentle"


# ---------------------------------------------------------------- 超過預算：暫停呼叫（第 10 項）

def test_over_call_budget_stops_every_model_call(base):
    llm = Scripted(verifier=verify_json("medium"))
    scores = {f"c{index}": 80 for index in range(5)}
    engine = make_engine(tuned(base, max_llm_calls_per_day=3), llm, scores=scores)
    engine.run(event(f"c{index}", index * 60) for index in range(5))
    assert len(llm.calls) == 4  # c0、c1 各 2 次（分流＋查證）；超過 3 次後一次都沒有
    assert engine.llm.paused and "超過預算" in engine.watchdog.pause_reason
    assert kinds(engine) == ["llm_paused"]
    assert [record_of(engine, f"c{index}").outcome for index in (2, 3, 4)] == ["paused"] * 3


def test_paused_candidates_are_still_reported_with_rules_only(base):
    engine = make_engine(tuned(base, max_llm_calls_per_day=1), Scripted(verifier=verify_json("medium")),
                         scores={"a": 80, "b": 80})
    engine.run([event("a", 0), event("b", 60)])
    report = engine.reports[-1]
    assert report.event_id == "b" and report.risk_level is None and not report.push
    assert report.rules == [RULE] and "模型暫停中" in report.summary


def test_token_budget_also_pauses(base):
    engine = make_engine(tuned(base, max_tokens_per_day=100), scores={})
    engine.process(event("a", 0))
    engine.llm.stats.prompt_tokens = 90
    engine.process(event("b", 60))
    assert not engine.llm.paused
    engine.llm.stats.completion_tokens = 20
    engine.process(event("c", 120))
    assert engine.llm.paused and "token" in engine.watchdog.pause_reason


def test_high_error_rate_pauses_and_releases_the_queue(base):
    llm = Scripted(router=RuntimeError("boom"))
    scores = {f"c{index}": 80 for index in range(4)}
    settings = tuned(base, min_calls_for_error_rate=3, max_error_rate=0.5)
    engine = make_engine(settings, llm, scores=scores)
    summary = engine.run(event(f"c{index}", index * 60) for index in range(4))
    assert llm.count("router") == 3
    assert engine.llm.paused and "錯誤率" in engine.watchdog.pause_reason
    assert summary.pending == 0 and len(engine.reports) == 4
    assert {record.outcome for record in engine.records} == {"paused"}


def test_a_single_failure_does_not_pause(base):
    llm = Scripted(verifier=[RuntimeError("boom"), verify_json("low")])
    engine = make_engine(tuned(base, min_calls_for_error_rate=10), llm, scores={"a": 80, "b": 80})
    engine.run([event("a", 0), event("b", 60), event("chat", 120)])
    assert not engine.llm.paused


def test_budget_resets_the_next_day(base):
    llm = Scripted(verifier=verify_json("medium"))
    engine = make_engine(tuned(base, max_llm_calls_per_day=1), llm, scores={"a": 80, "b": 80, "c": 80})
    engine.process(event("a", 0))
    engine.process(event("b", 60))
    assert engine.llm.paused
    calls = len(llm.calls)
    engine.process(event("c", DAY))
    assert not engine.llm.paused and engine.watchdog.pause_reason is None
    assert len(llm.calls) == calls + 2
    assert kinds(engine) == ["llm_paused", "llm_resumed"]


def test_client_gate_blocks_without_counting_a_call_or_an_error(base):
    client = LLMClient("offline", base.models)
    client.paused = True
    with pytest.raises(LLMPausedError):
        call_llm("router", "系統", "{}", client=client)
    assert client.stats.total_calls == 0 and client.stats.total_errors == 0


def paused_engine(config, llm=None, **kwargs):
    engine = make_engine(config, llm, **kwargs)
    engine.llm.paused = True
    return engine


def test_blocklist_hit_still_works_while_paused(base):
    llm = Scripted()
    fast_filter = FastFilter(RuleSet.load(), DomainList({"bad.example": "fake_investment"}))
    engine = paused_engine(base, llm, fast_filter=fast_filter)
    engine.speaking_mode = "gentle"
    engine.run([event("x", 0, text="大家看看 https://login.bad.example/x")])
    assert llm.calls == []
    assert engine.reports[0].push and BLOCKLIST_REASON in engine.reports[0].reasons
    reply = engine.replies[0]
    assert (reply.text, reply.source) == (REPLY_TEMPLATE, "template") and reply.card is not None
    assert any(entry.message == "模型暫停中，改用模板" for entry in engine.trace)


def test_watch_band_bothers_nobody_while_paused(base):
    llm = Scripted()
    engine = paused_engine(base, llm, scores={"w": 45})
    engine.run([event("w", 0)])
    assert llm.calls == [] and engine.reports == [] and len(engine.threads) == 0
    assert record_of(engine, "w").outcome == "paused"


@pytest.mark.parametrize("score,text", [(0, NEUTRAL_TEMPLATE), (45, UNVERIFIED_TEMPLATE), (80, UNVERIFIED_TEMPLATE)])
def test_mention_is_answered_without_a_model_while_paused(base, score, text):
    llm = Scripted()
    engine = paused_engine(base, llm, scores={"q": score})
    engine.run([event("q", 0, mention=True)])
    assert llm.calls == [] and [reply.text for reply in engine.replies] == [text]


def test_queued_mention_is_answered_when_the_pause_releases_the_queue(base):
    llm = Scripted(verifier=verify_json("medium"))
    settings = tuned(base, max_llm_calls_per_day=1, check_every_messages=3)
    engine = make_engine(settings, llm, scores={"a": 80, "ask": 80})
    engine.process(event("a", 0))
    engine.process(event("ask", 5, mention=True))  # Router 還在忙，留在佇列
    assert [item.trigger.id for item in engine.candidate_queue] == ["ask"]
    engine.process(event("chat", 10))
    assert engine.llm.paused and engine.candidate_queue == []
    assert [reply.text for reply in engine.replies] == [UNVERIFIED_TEMPLATE]


# ---------------------------------------------------------------- 鬼打牆：結束 Thread

def test_looping_thread_is_closed_and_stops_costing_tokens(base):
    llm = Scripted(verifier=verify_json("medium"))
    scores = {"w": 45, "n1": 10, "n2": 10, "n3": 10, "n4": 10}
    engine = make_engine(tuned(base, max_thread_verifies=2), llm, scores=scores)
    engine.run([event("w", 0), event("n1", 60), event("n2", 120), event("n3", 180), event("n4", 240)])
    assert kinds(engine) == ["thread_closed"]
    assert len(engine.threads) == 0
    assert llm.count("verifier") == 4  # w 和 n1–n3；n4 進來時 Thread 已經結束


def test_closed_thread_does_not_reopen_until_it_would_have_expired(base):
    llm = Scripted(verifier=verify_json("medium"))
    engine = make_engine(tuned(base, max_thread_verifies=1), llm, scores={"w": 45, "n1": 10, "n2": 10, "w2": 45,
                                                                         "n3": 10, "w3": 45})
    engine.run([event("w", 0), event("n1", 60), event("n2", 120), event("n3", 180)])
    assert len(engine.threads) == 0
    engine.process(event("w2", 240))  # 觀察區間照樣查證，但不重開 Thread
    assert len(engine.threads) == 0 and record_of(engine, "w2") is not None
    engine.process(event("w3", DAY + 60))  # 過了原本的存活時間就可以再開
    assert len(engine.threads) == 1


def test_notice_about_a_thread_does_not_reveal_the_domain_or_sender(base):
    llm = Scripted(verifier=verify_json("medium"))
    text = "看 https://bad.example/a"
    engine = make_engine(tuned(base, max_thread_verifies=1), llm, scores={"w": 45})
    engine.run(event(name, index * 60, text=text) for index, name in enumerate(["w", "n1", "n2", "n3"]))
    assert kinds(engine) == ["thread_closed"]
    assert "bad.example" not in engine.notices[0].message and "u_" not in engine.notices[0].message


# ---------------------------------------------------------------- 推播額度

def test_push_gate_only_lets_high_through(base):
    engine = make_engine(base)
    assert engine.watchdog.claim_push("high", BASE_TIME)
    for level in ("medium", "low", None):
        assert not engine.watchdog.claim_push(level, BASE_TIME)
    assert engine.watchdog.push_used == 1


def test_near_quota_notifies_once_and_keeps_pushing_high(base):
    scores = {f"c{index}": 80 for index in range(4)}
    engine = make_engine(tuned(base, push_quota_monthly=10, push_warn_ratio=0.2),
                         Scripted(verifier=verify_json("high")), scores=scores)
    engine.run(event(f"c{index}", index * 60) for index in range(4))
    assert all(report.push for report in engine.reports)
    assert kinds(engine) == ["push_near_quota"]
    assert "2／10" in engine.notices[0].message


def test_used_up_quota_stops_push_but_not_the_report(base):
    scores = {"a": 80, "b": 80}
    engine = make_engine(tuned(base, push_quota_monthly=1), Scripted(verifier=verify_json("high")), scores=scores)
    engine.run([event("a", 0), event("b", 60)])
    assert [report.push for report in engine.reports] == [True, False]
    assert engine.watchdog.push_used == 1
    assert any(entry.stage == "reporter" and not entry.data["push"] and entry.event_id == "b"
               for entry in engine.trace)


def test_push_count_resets_next_month(base):
    engine = make_engine(tuned(base, push_quota_monthly=1), Scripted(verifier=verify_json("high")),
                         scores={"a": 80, "b": 80})
    engine.run([event("a", 0), event("b", 40 * DAY)])
    assert [report.push for report in engine.reports] == [True, True]


def test_medium_reports_never_use_the_quota(base):
    engine = make_engine(base, Scripted(verifier=verify_json("medium")), scores={"a": 80})
    engine.run([event("a", 0)])
    assert not engine.reports[0].push and engine.watchdog.push_used == 0


# ---------------------------------------------------------------- 卡在佇列的候選（決策 D16、D29）

def test_stuck_candidates_get_one_retry_then_a_rules_only_report(base):
    llm = Scripted(router=RuntimeError("boom"))
    engine = make_engine(tuned(base, check_every_messages=2), llm, scores={"a": 80})
    events = [event("a", 0)] + [event(f"chat{index}", (index + 1) * 60) for index in range(5)]
    summary = engine.run(events)
    assert llm.count("router") == 2  # 原本一次加重試一次，不會一直試
    assert summary.pending == 0
    report = engine.reports[0]
    assert report.risk_level is None and "Router 一直失敗" in report.summary
    assert record_of(engine, "a").outcome == "router_failed"
    assert kinds(engine) == ["router_stuck"]


def test_retry_that_succeeds_clears_the_queue_quietly(base):
    llm = Scripted(router=[RuntimeError("boom"), route_all("ignore")])
    engine = make_engine(tuned(base, check_every_messages=2), llm, scores={"a": 80})
    summary = engine.run([event("a", 0), event("chat1", 60), event("chat2", 120), event("chat3", 180)])
    assert summary.pending == 0 and engine.notices == [] and engine.reports == []
    assert record_of(engine, "a").outcome == "ignored"


def test_new_candidates_are_tried_by_the_router_not_the_watchdog(base):
    llm = Scripted(router=[RuntimeError("boom"), route_all("ignore")])
    engine = make_engine(tuned(base, check_every_messages=100), llm, scores={"a": 80, "b": 80})
    engine.run([event("a", 0), event("b", 60)])
    assert llm.batches() == [["a"], ["a", "b"]]
    assert not any(entry.stage == "watchdog" for entry in engine.trace)


# ---------------------------------------------------------------- 指標

def test_snapshot_has_what_the_simulator_shows(base):
    engine = make_engine(base, Scripted(verifier=verify_json("high")), scores={"c": 80})
    engine.speaking_mode = "gentle"
    engine.run([event("c", 0)])
    snapshot = engine.watchdog.snapshot()
    assert snapshot["reminders_last_100"] == 1 and snapshot["max_reminders_per_100"] == 5
    assert snapshot["llm_calls_today"] == 4 and snapshot["llm_errors_today"] == 0  # 分流、查證、摘要、起草
    assert snapshot["push_used"] == 1 and snapshot["push_quota"] == 200
    assert snapshot["llm_paused"] is False and snapshot["speaking_mode"] == "gentle"


# ---------------------------------------------------------------- 今日摘要

def test_daily_summary_lists_events_without_calling_the_model(base):
    llm = Scripted()
    engine = make_engine(base, llm)
    engine.run(load_scenario(DEMO).raw_events())
    calls = len(llm.calls)
    text = engine.daily_summary()
    assert len(llm.calls) == calls
    day = engine.records[0].time
    assert text.startswith(f"顧家貓今日摘要（{day:%m/%d}）")
    assert "訊息 20 則" in text  # 21 個事件裡有 1 個是新成員入群，不算訊息
    assert "風險事件 6 則（高 6、中 0、查證未完成 0、其他 0）" in text
    assert "發言模式：silent" in text and "高風險：" in text
    for record in engine.records:
        assert record.event_id in text
    assert "誤報" in text


def test_daily_summary_has_no_message_text_or_links(base):
    secret = "老師帶單保證獲利穩賺不賠快來 https://bad.example/join"
    engine = make_engine(base, Scripted(reporter=secret), scores={"c": 80})
    engine.run([event("c", 0, text=secret)])
    text = engine.daily_summary()
    assert "保證獲利穩賺" not in text and "bad.example" not in text and "c" in text


def test_daily_summary_only_counts_that_day(base):
    engine = make_engine(base, Scripted(verifier=verify_json("medium")), scores={"a": 80, "b": 80})
    engine.run([event("a", 0), event("b", DAY)])
    today = engine.daily_summary()
    assert "風險事件 1 則（高 0、中 1" in today and "- b " in today and "- a " not in today
    assert "訊息 1 則" in today
    yesterday = engine.daily_summary(BASE_TIME.date())
    assert "- a " in yesterday and "- b " not in yesterday
    assert "模型呼叫 0 次" in yesterday  # 用量只留當天的


def test_unfinished_verification_is_listed_on_its_own(base):
    engine = make_engine(base, Scripted(verifier=RuntimeError("boom")), scores={"a": 80})
    engine.run([event("a", 0)])
    text = engine.daily_summary()
    assert "查證未完成 1" in text and "查證未完成：" in text and "- a " in text


def test_notices_show_up_once_even_on_a_later_day(base):
    engine = make_engine(tuned(base, max_reminders_per_100=0), Scripted(verifier=verify_json("high")),
                         scores={"c": 80})
    engine.speaking_mode = "gentle"
    engine.run([event("c", 0), event("chat", 60)])
    later = BASE_TIME.date() + timedelta(days=3)
    first = engine.daily_summary(later)
    assert "Watchdog 通知：" in first and "切回 silent" in first and "發言模式：silent" in first
    assert "Watchdog 通知：" not in engine.daily_summary(later)


def test_daily_summary_shows_the_pause(base):
    engine = make_engine(tuned(base, max_llm_calls_per_day=1), Scripted(verifier=verify_json("medium")),
                         scores={"a": 80, "b": 80})
    engine.run([event("a", 0), event("b", 60)])
    assert "模型暫停中：今天呼叫模型" in engine.daily_summary()


def test_daily_summary_stays_within_the_line_limit(base):
    scores = {f"c{index}": 80 for index in range(60)}
    engine = make_engine(dataclasses.replace(base, watchdog=dataclasses.replace(base.watchdog,
                                                                               max_llm_calls_per_day=10_000)),
                         Scripted(verifier=verify_json("medium")), scores=scores)
    engine.run(event(f"c{index}", index * 60) for index in range(60))
    text = engine.daily_summary()
    assert "還有 40 則" in text and len(text) <= 4900


def test_daily_summary_with_no_events(base):
    text = make_engine(base).daily_summary()
    assert "訊息 0 則；風險事件 0 則" in text and "誤報" not in text
