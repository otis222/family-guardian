"""LINE 伺服器的核心（SPEC 第 5、10 節）：不連 LINE，用假的傳送層驗證送出去的內容和花費。"""

import json
import logging

import pytest
from helpers import Scripted, route_all, verify_json
from line_helpers import (
    CANDIDATE,
    CHAT,
    GROUP,
    GUARDIAN,
    MEMBER,
    OTHER_GROUP,
    PUBLIC_URL,
    SECOND_GUARDIAN,
    STRANGER,
    WATCH,
    Harness,
)

from guardian.line_bot import (
    HELP_TEXT,
    PAUSED_TEMPLATE,
    RESULT_EXPIRED_TEXT,
    STILL_CHECKING_TEXT,
    ButtonOut,
    ImageOut,
    TextOut,
)
from guardian.models import RECORD_FIELDS, GuardianNotice
from guardian.router import NEUTRAL_TEMPLATE, UNVERIFIED_TEMPLATE


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def high(tmp_path, **kwargs):
    llm = Scripted(router=route_all("verify"), verifier=verify_json("high"))
    return Harness(tmp_path, llm, **kwargs)


def medium(tmp_path, **kwargs):
    llm = Scripted(router=route_all("verify"), verifier=verify_json("medium"))
    return Harness(tmp_path, llm, **kwargs)


# ---------------------------------------------------------------- Phase 6 驗收：silent 模式的報告

def test_silent_mode_reports_to_the_guardian_and_stays_quiet_in_the_group(tmp_path):
    h = high(tmp_path)
    h.send(h.group_text(CANDIDATE))
    assert h.transport.replies == []
    (to, messages), = h.transport.pushes
    assert to == GUARDIAN
    text = messages[0].text
    assert "風險高" in text and "要求轉帳或 ATM 操作" in text and "事件編號" in text
    assert h.engine.watchdog.push_used == 1


def test_the_report_has_no_message_text_or_links(tmp_path):
    h = high(tmp_path)
    secret = "老師帶單保證獲利穩賺不賠快來 https://bad.example/join"
    h.send(h.group_text(secret))
    pushed = h.transport.pushes[0][1][0].text
    assert "穩賺不賠快來" not in pushed and "bad.example" not in pushed


def test_medium_waits_for_the_daily_summary_and_costs_nothing(tmp_path):
    h = medium(tmp_path)
    h.send(h.group_text(CANDIDATE))
    assert h.transport.pushes == [] and h.engine.watchdog.push_used == 0
    assert "中風險：" in h.command("今日摘要")


def test_chat_makes_no_model_calls_and_sends_nothing(harness):
    harness.send(harness.group_text(CHAT))
    assert harness.llm.calls == [] and harness.transport.replies == [] and harness.transport.pushes == []


def test_daily_summary_is_a_free_reply(tmp_path):
    h = high(tmp_path)
    h.send(h.group_text(CANDIDATE))
    pushes = len(h.transport.pushes)
    used = h.engine.watchdog.push_used
    summary = h.command("今日摘要")
    assert summary.startswith("顧家貓今日摘要") and "高風險：" in summary
    assert len(h.transport.pushes) == pushes and h.engine.watchdog.push_used == used


def test_each_guardian_counts_against_the_quota(tmp_path):
    h = high(tmp_path, guardians=(GUARDIAN, SECOND_GUARDIAN))
    h.send(h.group_text(CANDIDATE))
    assert [to for to, _ in h.transport.pushes] == [GUARDIAN, SECOND_GUARDIAN]
    assert h.engine.watchdog.push_used == 2


def test_watchdog_notice_travels_with_the_next_push_for_free(tmp_path):
    h = high(tmp_path)
    h.engine.add_notice("tick", GuardianNotice(h.clock.now, "test", "測試通知"))
    h.send(h.group_text(CANDIDATE))
    assert "Watchdog 通知" in h.transport.pushes[0][1][0].text and "測試通知" in h.transport.pushes[0][1][0].text
    assert h.engine.watchdog.push_used == 1
    assert all(notice.delivered for notice in h.engine.notices)


# ---------------------------------------------------------------- 群組回覆：reply 優先（零成本）

def test_gentle_high_uses_the_reply_token_with_text_and_card(tmp_path):
    h = high(tmp_path, mode="gentle")
    h.send(h.group_text(CANDIDATE, token="the-token"))
    (token, messages), = h.transport.replies
    assert token == "the-token"
    assert isinstance(messages[0], TextOut) and messages[1] == ImageOut(PUBLIC_URL + "/card/warning.png")
    assert [to for to, _ in h.transport.pushes] == [GUARDIAN]  # 群組沒有用 push


def test_without_a_public_url_only_the_text_is_sent(tmp_path):
    h = high(tmp_path, mode="gentle", public_url=None)
    h.send(h.group_text(CANDIDATE))
    assert [type(message) for message in h.transport.replies[0][1]] == [TextOut]


def test_medium_in_gentle_mode_does_not_speak(tmp_path):
    h = medium(tmp_path, mode="gentle")
    h.send(h.group_text(CANDIDATE))
    assert h.transport.replies == []


def test_mention_gets_an_answer_even_in_silent_mode(harness):
    harness.send(harness.group_text("@顧家貓 " + CHAT, mention=True))
    assert harness.transport.texts() == [NEUTRAL_TEMPLATE]
    assert harness.llm.calls == []


def test_mention_of_someone_else_is_not_a_mention(harness):
    harness.send(harness.group_text("@爸爸 " + CHAT, others_mentioned=True))
    assert harness.transport.replies == []


def test_router_failure_still_answers_a_mention(tmp_path):
    h = Harness(tmp_path, Scripted(router=RuntimeError("boom")))
    h.send(h.group_text(CANDIDATE, mention=True))
    assert h.transport.texts() == [UNVERIFIED_TEMPLATE]


# ---------------------------------------------------------------- 查證來不及：按鈕和 postback

def test_slow_verification_sends_a_button_before_the_token_expires(tmp_path):
    h = high(tmp_path)
    h.bot.enqueue([h.group_text(CANDIDATE, mention=True, token="first")])  # 工作執行緒還卡在別的事
    h.clock.advance(seconds=46)
    h.bot.check_deadlines()
    (token, messages), = h.transport.replies
    assert token == "first" and isinstance(messages[0], ButtonOut)
    assert messages[0].data.startswith("result:")

    h.bot.process_pending()  # 查證完了，但 token 已經用掉
    assert len(h.transport.replies) == 1

    h.send(h.postback(messages[0].data))  # 使用者點按鈕，帶來新的 replyToken，免費送出結果
    token, result = h.transport.replies[1]
    assert token != "first" and isinstance(result[0], TextOut) and isinstance(result[1], ImageOut)
    assert h.engine.watchdog.push_used == 1  # 只有給守護人的那一次


def test_button_click_before_the_result_is_ready_asks_to_wait(harness):
    harness.bot.enqueue([harness.group_text(CHAT, mention=True)])
    harness.clock.advance(seconds=46)
    harness.bot.check_deadlines()
    data = harness.transport.replies[0][1][0].data
    harness.send(harness.postback(data))  # 按鈕比查證先處理
    assert harness.transport.replies[1][1][0].text == STILL_CHECKING_TEXT
    harness.send(harness.postback(data))  # 再點一次就有結果了
    assert harness.transport.replies[2][1][0].text == NEUTRAL_TEMPLATE


def test_old_or_unknown_button_says_it_expired(tmp_path):
    h = high(tmp_path)
    h.bot.enqueue([h.group_text(CANDIDATE, mention=True)])
    h.clock.advance(seconds=46)
    h.bot.check_deadlines()
    data = h.transport.replies[0][1][0].data
    h.bot.process_pending()
    h.clock.advance(hours=25)
    h.bot.check_deadlines()
    h.send(h.postback(data))
    assert h.transport.replies[-1][1][0].text == RESULT_EXPIRED_TEXT
    h.send(h.postback("result:nothing"))
    assert h.transport.replies[-1][1][0].text == RESULT_EXPIRED_TEXT


def test_unrelated_postback_is_ignored(harness):
    harness.send(harness.postback("something-else"))
    assert harness.transport.replies == []


def test_no_button_for_messages_nobody_asked_about(tmp_path):
    h = high(tmp_path, mode="gentle")
    h.bot.enqueue([h.group_text(CANDIDATE)])
    h.clock.advance(seconds=46)
    h.bot.check_deadlines()
    assert h.transport.replies == []  # gentle 只在確定 high 時才開口，不先發按鈕


def test_answered_mention_gets_no_button(harness):
    harness.send(harness.group_text(CHAT, mention=True))
    harness.clock.advance(seconds=46)
    harness.bot.check_deadlines()
    assert len(harness.transport.replies) == 1


def test_a_failed_button_is_recorded_and_does_not_wait_for_a_click(tmp_path):
    h = high(tmp_path)
    h.bot.enqueue([h.group_text(CANDIDATE, mention=True)])
    h.transport.fail_reply = True
    h.clock.advance(seconds=46)
    h.bot.check_deadlines()
    assert h.engine.counts["errors:line_reply"] == 1
    h.transport.fail_reply = False
    h.bot.process_pending()
    assert [to for to, _ in h.transport.pushes] == [GROUP, GUARDIAN]  # high：不得已才推到群組


def test_expired_tokens_are_dropped_from_memory(harness):
    harness.bot.enqueue([harness.group_text(CHAT, token="secret-token")])
    harness.clock.advance(seconds=56)
    harness.bot.check_deadlines()
    assert all(pending.token is None for pending in harness.bot._pending.values())


# ---------------------------------------------------------------- 不得已才用 push

def test_late_high_is_pushed_counting_every_group_member(tmp_path):
    h = high(tmp_path, mode="gentle")
    h.bot.enqueue([h.group_text(CANDIDATE)])
    h.clock.advance(seconds=56)
    h.bot.process_pending()
    assert h.transport.replies == []
    assert [to for to, _ in h.transport.pushes] == [GROUP, GUARDIAN]
    assert h.engine.watchdog.push_used == 1 + h.transport.size  # LINE 依收件人計算


def test_late_answer_that_is_not_high_is_dropped_instead_of_pushed(harness):
    harness.bot.enqueue([harness.group_text(CHAT, mention=True)])
    harness.clock.advance(seconds=56)
    harness.bot.process_pending()
    assert harness.transport.pushes == []
    assert harness.engine.counts["line:reply_dropped"] == 1


def test_quota_too_small_for_the_group_drops_the_push(tmp_path):
    h = high(tmp_path, mode="gentle")
    h.transport.size = 500
    h.bot.enqueue([h.group_text(CANDIDATE)])
    h.clock.advance(seconds=56)
    h.bot.process_pending()
    assert [to for to, _ in h.transport.pushes] == [GUARDIAN]  # 守護人照樣收到報告
    assert h.engine.counts["line:reply_dropped"] == 1


def test_failed_reply_falls_back_to_push_only_for_high(tmp_path):
    h = high(tmp_path, mode="gentle")
    h.transport.fail_reply = True
    h.send(h.group_text(CANDIDATE))
    assert h.engine.counts["errors:line_reply"] == 1
    assert [to for to, _ in h.transport.pushes] == [GROUP, GUARDIAN]


def test_failed_push_does_not_stop_the_worker(tmp_path):
    h = high(tmp_path)
    h.transport.fail_push = True
    h.send(h.group_text(CANDIDATE))
    assert h.engine.counts["errors:line_push"] == 1
    h.transport.fail_push = False
    h.send(h.group_text(CANDIDATE))
    assert len(h.transport.pushes) == 1


def test_used_up_quota_stops_pushing_reports(tmp_path):
    h = high(tmp_path)
    h.engine.watchdog.sync_push_usage(h.config.watchdog.push_quota_monthly)
    h.send(h.group_text(CANDIDATE))
    assert h.transport.pushes == []
    assert "高風險：" in h.command("今日摘要")


# ---------------------------------------------------------------- 批次（SPEC 3-3）

def test_events_that_piled_up_during_inference_merge_into_one_router_call(tmp_path):
    h = medium(tmp_path)
    h.bot.enqueue([h.group_text(CANDIDATE), h.group_text(CANDIDATE), h.group_text(CHAT), h.group_text(CANDIDATE)])
    h.bot.process_pending()
    assert [len(batch) for batch in h.llm.batches()] == [3]
    assert h.llm.count("verifier") == 3


def test_candidates_arriving_one_by_one_do_not_wait_for_a_simulated_delay(tmp_path):
    h = medium(tmp_path)
    h.send(h.group_text(CANDIDATE))
    h.clock.advance(seconds=1)
    h.send(h.group_text(CANDIDATE))
    assert [len(batch) for batch in h.llm.batches()] == [1, 1]


def test_a_quiet_tick_moves_things_along(tmp_path):
    h = Harness(tmp_path, Scripted(router=[RuntimeError("boom"), route_all("ignore")]))
    h.send(h.group_text(CANDIDATE))
    assert len(h.engine.candidate_queue) == 1
    h.clock.advance(minutes=11)
    h.bot.process_pending()  # 沒有新訊息，Watchdog 定時檢查時重試一次
    assert h.engine.candidate_queue == [] and h.llm.count("router", "route") == 2


# ---------------------------------------------------------------- 哪些事件要處理

def test_group_that_is_not_allowed_is_ignored_completely(harness, caplog):
    secret = "只有這個群組的人看得到的內容"
    with caplog.at_level(logging.INFO, logger="guardian.line"):
        harness.send(harness.group_text(CANDIDATE, group=OTHER_GROUP),
                     harness.group_text(secret, group=OTHER_GROUP, mention=True))
    assert harness.llm.calls == [] and harness.transport.replies == []
    assert harness.engine.records == [] and harness.engine.counts["events"] == 0
    assert OTHER_GROUP in caplog.text and secret not in caplog.text


def test_joining_a_new_group_prints_its_id_and_stays_quiet(harness, caplog):
    with caplog.at_level(logging.INFO, logger="guardian.line"):
        harness.send({"type": "join", "timestamp": harness.timestamp(), "replyToken": "t",
                      "source": {"type": "group", "groupId": OTHER_GROUP}})
    assert OTHER_GROUP in caplog.text and "GUARDIAN_GROUP_IDS" in caplog.text
    assert harness.transport.replies == []


def test_joining_an_allowed_group_only_counts(harness):
    harness.send({"type": "join", "timestamp": harness.timestamp(), "replyToken": "t",
                  "source": {"type": "group", "groupId": GROUP}})
    assert harness.engine.counts["kind:join"] == 1 and harness.transport.replies == []


def test_member_joined_feeds_the_new_member_rule(tmp_path):
    h = medium(tmp_path)
    newcomer = "U" + "9" * 32
    h.send({"type": "memberJoined", "timestamp": h.timestamp(), "replyToken": "t",
            "source": {"type": "group", "groupId": GROUP},
            "joined": {"members": [{"type": "user", "userId": newcomer}]}})
    h.clock.advance(minutes=2)
    h.send(h.group_text("大家看看 https://x.example/a", user=newcomer))
    assert "新成員剛入群就發連結" in h.engine.records[0].rules


def test_images_are_only_counted(harness):
    harness.send({"type": "message", "timestamp": harness.timestamp(), "replyToken": "t",
                  "source": {"type": "group", "groupId": GROUP, "userId": MEMBER},
                  "message": {"type": "image", "id": "3"}})
    assert harness.engine.counts["kind:image"] == 1 and harness.llm.calls == []


@pytest.mark.parametrize("event", [
    {"type": "message", "source": {"type": "group", "groupId": GROUP, "userId": MEMBER},
     "message": {"type": "sticker", "id": "4"}},
    {"type": "message", "source": {"type": "room", "roomId": "R1", "userId": MEMBER},
     "message": {"type": "text", "text": CANDIDATE}},
    {"type": "follow", "source": {"type": "user", "userId": MEMBER}},
    {"type": "leave", "source": {"type": "group", "groupId": GROUP}},
    {"type": "message", "source": {"type": "group", "groupId": GROUP, "userId": MEMBER},
     "message": {"type": "text", "text": 123}},
    {"source": None},
    "這不是物件",
])
def test_other_events_are_skipped_without_trouble(harness, event):
    harness.send(event)
    assert harness.engine.counts["events"] == 0 and harness.transport.replies == []


def test_message_without_a_sender_id_is_still_checked(tmp_path):
    h = medium(tmp_path)
    event = h.group_text(CANDIDATE)
    del event["source"]["userId"]
    h.send(event)
    assert len(h.engine.records) == 1 and h.engine.records[0].sender_hash


def test_redelivered_event_is_processed_once(tmp_path):
    h = high(tmp_path)
    event = {**h.group_text(CANDIDATE), "webhookEventId": "01ABC", "deliveryContext": {"isRedelivery": True}}
    h.send(event)
    h.send(event)
    assert len(h.transport.pushes) == 1


def test_event_ids_are_short_and_unique_within_a_second(tmp_path):
    h = medium(tmp_path)
    h.send(h.group_text(CANDIDATE), h.group_text(CANDIDATE), h.group_text(CANDIDATE))
    ids = [record.event_id for record in h.engine.records]
    assert len(set(ids)) == 3 and len(ids[0]) == 11
    assert ids[0] == h.clock.now.strftime("%m%d-%H%M%S")


def test_event_time_comes_from_line(tmp_path):
    h = medium(tmp_path)
    event = h.group_text(CANDIDATE)
    h.clock.advance(seconds=30)
    h.send(event)
    assert h.engine.records[0].time.timestamp() == event["timestamp"] / 1000


# ---------------------------------------------------------------- 管理指令（SPEC 第 5 節）

def test_only_guardians_can_give_commands(harness):
    reply = harness.command("暫停", user=STRANGER)
    assert STRANGER in reply and GUARDIAN not in reply  # 只看得到自己的編號
    assert harness.bot.paused is False


def test_direct_messages_never_reach_the_engine_or_the_model(harness):
    harness.command(CANDIDATE)
    harness.command(CANDIDATE, user=STRANGER)
    assert harness.llm.calls == [] and harness.engine.counts["events"] == 0 and harness.engine.records == []


def test_commands_in_the_group_are_just_messages(harness):
    harness.send(harness.group_text("暫停", user=GUARDIAN))
    assert harness.bot.paused is False and harness.transport.replies == []


def test_unknown_command_lists_what_is_available(harness):
    assert harness.command("你好") == HELP_TEXT
    assert harness.command("") == HELP_TEXT
    assert harness.command("今日摘要 拜託") == HELP_TEXT


def test_pause_stops_everything_but_a_mention_still_gets_an_answer(tmp_path):
    h = high(tmp_path)
    assert "暫停了" in h.command("暫停")
    h.send(h.group_text(CANDIDATE), h.group_text(CANDIDATE, mention=True))
    assert h.llm.calls == [] and h.engine.records == [] and h.transport.pushes == []
    assert h.transport.replies[-1][1][0].text == PAUSED_TEMPLATE


def test_pause_does_not_leave_candidates_waiting_for_the_model(tmp_path):
    h = Harness(tmp_path, Scripted(router=RuntimeError("boom")))
    h.send(h.group_text(CANDIDATE))
    assert len(h.engine.candidate_queue) == 1
    h.command("暫停")
    assert h.engine.candidate_queue == [] and h.engine.records[0].outcome == "paused"


def test_resume_goes_back_to_work(tmp_path):
    h = high(tmp_path)
    h.command("暫停")
    assert "恢復了" in h.command("恢復")
    h.send(h.group_text(CANDIDATE))
    assert len(h.transport.pushes) == 1


@pytest.mark.parametrize("text", ["模式 gentle", "模式gentle", "模式　GENTLE", " 模式  gentle "])
def test_mode_command_changes_how_the_bot_speaks(tmp_path, text):
    h = high(tmp_path)
    assert "gentle" in h.command(text)
    assert h.engine.speaking_mode == "gentle"
    h.send(h.group_text(CANDIDATE))
    assert "詐騙常見的特徵" in h.transport.replies[-1][1][0].text


@pytest.mark.parametrize("text", ["模式", "模式 loud"])
def test_bad_mode_changes_nothing(harness, text):
    assert "只能是" in harness.command(text)
    assert harness.engine.speaking_mode == "silent"


def test_false_positive_is_marked_and_shows_up_in_the_summary(tmp_path):
    h = high(tmp_path)
    h.send(h.group_text(CANDIDATE))
    event_id = h.engine.records[0].event_id
    assert event_id in h.command(f"誤報 {event_id}")
    assert h.engine.watchdog.snapshot()["false_positives"] == 1
    assert "守護人標記誤報 1 則" in h.command("今日摘要")


@pytest.mark.parametrize("text", ["誤報", "誤報 不存在的編號"])
def test_false_positive_needs_a_real_event(harness, text):
    assert "編號" in harness.command(text)
    assert harness.engine.false_positives == set()


def test_summary_uses_the_official_push_count_and_survives_a_failed_lookup(harness):
    harness.transport.usage = 37
    assert "本月推播 37／200 則" in harness.command("今日摘要")
    harness.transport.fail_usage = True
    assert "本月推播 37／200 則" in harness.command("今日摘要")


def test_summary_says_when_the_bot_is_paused(harness):
    harness.command("暫停")
    assert "暫停中" in harness.command("今日摘要")


def test_reply_failure_on_a_command_is_only_counted(harness):
    harness.transport.fail_reply = True
    harness.send(harness.direct_text("今日摘要"))
    assert harness.engine.counts["errors:line_reply"] == 1


# ---------------------------------------------------------------- 紀錄和重新啟動（SPEC 第 7 節）

def test_records_on_disk_are_minimal(tmp_path):
    h = high(tmp_path)
    secret = "老師帶單保證獲利穩賺不賠快來 https://bad.example/join"
    h.send(h.group_text(secret, token="secret-token", user=MEMBER))
    files = list((tmp_path / "records").iterdir())
    assert files
    dump = "".join(path.read_text(encoding="utf-8") for path in files)
    for forbidden in ("穩賺不賠快來", "bad.example", "secret-token", MEMBER, GROUP):
        assert forbidden not in dump
    lines = (tmp_path / "records" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert all(set(json.loads(line)) == set(RECORD_FIELDS) for line in lines)


def test_restart_brings_back_records_mode_and_pause(tmp_path):
    h = high(tmp_path)
    h.send(h.group_text(CANDIDATE))
    event_id = h.engine.records[0].event_id
    h.command(f"誤報 {event_id}")
    h.command("模式 gentle")
    h.command("暫停")

    restarted = h.new_bot()
    assert restarted.paused is True and restarted.engine.speaking_mode == "gentle"
    assert [record.event_id for record in restarted.engine.records] == [event_id]
    assert restarted.engine.false_positives == {event_id}
    assert restarted.engine.records[0].risk_level == "high"


def test_default_is_silent_when_nothing_was_saved(harness):
    assert harness.new_bot().engine.speaking_mode == harness.config.speaking_mode == "silent"


def test_records_older_than_the_retention_period_disappear(tmp_path):
    h = high(tmp_path)
    h.send(h.group_text(CANDIDATE))
    event_id = h.engine.records[0].event_id
    h.command(f"誤報 {event_id}")
    h.clock.advance(days=h.config.record_retention_days + 1)
    h.bot.process_pending()
    assert h.engine.records == [] and h.engine.false_positives == set()
    dump = (tmp_path / "records" / "events.jsonl").read_text(encoding="utf-8")
    assert event_id not in dump
    assert h.new_bot().engine.records == []


def test_a_failed_state_save_still_changes_the_mode_and_answers(tmp_path, monkeypatch):
    h = high(tmp_path)

    def broken(state):
        raise PermissionError("存取被拒")

    monkeypatch.setattr(h.bot.store, "save_state", broken)
    assert "暫停了" in h.command("暫停")
    assert h.bot.paused is True and h.engine.counts["errors:records"] == 1


def test_a_failed_save_is_counted_and_the_bot_keeps_going(tmp_path, monkeypatch):
    h = high(tmp_path)

    def broken(records):
        raise OSError("磁碟滿了")

    monkeypatch.setattr(h.bot.store, "save", broken)
    h.send(h.group_text(CANDIDATE))
    assert h.engine.counts["errors:records"] == 1 and len(h.transport.pushes) == 1


# ---------------------------------------------------------------- 日誌和例外

def test_logs_never_contain_message_text(tmp_path, caplog):
    h = Harness(tmp_path, Scripted(router=RuntimeError("boom " + CANDIDATE)))
    h.transport.fail_reply = True
    h.transport.fail_push = True
    with caplog.at_level(logging.DEBUG, logger="guardian.line"):
        h.send(h.group_text(CANDIDATE, mention=True))
        h.send(h.direct_text("今日摘要"))
    assert "解除分期" not in caplog.text and "內容不該進日誌" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_an_unexpected_error_does_not_stop_the_worker(harness, monkeypatch, caplog):
    def explode(raws):
        list(raws)
        raise RuntimeError("意外，訊息內容不該進日誌")

    monkeypatch.setattr(harness.engine, "process_batch", explode)
    with caplog.at_level(logging.ERROR, logger="guardian.line"):
        harness.send(harness.group_text(CHAT))
    assert harness.engine.counts["errors:line_bot"] == 1
    assert "內容不該進日誌" not in caplog.text
    monkeypatch.undo()
    harness.send(harness.group_text(CHAT))
    assert harness.engine.counts["events"] == 1


def test_masking_still_happens_before_the_model(tmp_path):
    h = high(tmp_path)
    h.send(h.group_text("我的電話 0912345678，" + CANDIDATE))
    assert h.llm.calls and all("0912345678" not in user for _, _, user in h.llm.calls)


def test_watch_band_mention_gets_a_reply_through_the_token(tmp_path):
    h = medium(tmp_path)
    h.send(h.group_text(WATCH, mention=True))
    assert "詐騙常見的特徵" in h.transport.texts()[0]  # 決策 D37
