"""模擬器的核心（SPEC 第 9 節）：一則一則跑，各欄的文字正確。"""

import dataclasses

import pytest
from helpers import Scripted, message, route_all, scenario_of, verify_json

from guardian.config import PROJECT_ROOT, load_config
from guardian.llm import LLMClient
from guardian.scenario import load_scenario
from guardian.simulation import Simulation, format_trace
from guardian.workers.card import warning_card_png

DEMO = PROJECT_ROOT / "scenarios" / "demo_family.yaml"


@pytest.fixture
def config():
    return load_config()


def finish(simulation):
    while simulation.step():
        pass
    return simulation


def test_each_step_advances_one_message(config):
    simulation = Simulation.from_path(config, DEMO)
    assert simulation.position == 0 and not simulation.finished and simulation.chat_text() == ""
    assert simulation.step() is True
    assert simulation.position == 1
    assert "阿嬤：大家早安" in simulation.chat_text()
    assert "第 1／21 則" in simulation.status_text() and "C Super-agent" in simulation.status_text()


def test_it_stops_at_the_end_and_flushes_the_queue(config):
    scenario = scenario_of(message("a", 0, text="請依客服指示到 ATM 操作解除分期"),
                           message("b", 5, text="請依客服指示到 ATM 操作解除分期"))
    llm = Scripted(router=route_all("verify"), verifier=verify_json("medium"))
    simulation = Simulation(config, scenario, llm_client=LLMClient("offline", config.models, responder=llm))
    assert simulation.step() is True
    assert simulation.step() is False  # 最後一則：Router 還在忙，但劇本結束就送出
    assert simulation.finished and "已跑完" in simulation.status_text()
    assert llm.batches() == [["a"], ["b"]] and len(simulation.engine.reports) == 2
    assert simulation.step() is False and simulation.position == 2


def test_group_panel_shows_replies_where_they_happened(config):
    simulation = finish(Simulation.from_path(config, DEMO))
    lines = simulation.chat_text().splitlines()
    asked = next(index for index, line in enumerate(lines) if "@顧家貓" in line)
    assert lines[asked + 1].strip().startswith("顧家貓：") and "（附圖卡）" in lines[asked + 1]
    assert "新成員 加入群組" in simulation.chat_text()
    assert "這則訊息有詐騙常見的特徵" in simulation.replies_text()


def test_card_appears_only_after_a_warning(config):
    simulation = Simulation.from_path(config, DEMO)
    assert simulation.latest_card() is None
    finish(simulation)
    assert simulation.latest_card() == warning_card_png()


def test_silent_and_gentle_differ_in_the_group_but_not_in_the_reports(config):
    silent = finish(Simulation.from_path(config, DEMO, speaking_mode="silent"))
    gentle = finish(Simulation.from_path(config, DEMO, speaking_mode="gentle"))
    assert len(silent.engine.replies) == 1 and len(gentle.engine.replies) == 6
    assert silent.reports_text() == gentle.reports_text()


def test_speaking_mode_can_change_halfway(config):
    simulation = Simulation.from_path(config, DEMO, speaking_mode="silent")
    for _ in range(4):
        simulation.step()
    assert simulation.engine.replies == []
    simulation.engine.speaking_mode = "gentle"
    finish(simulation)
    assert "m03" not in simulation.replies_text() and "m06" in simulation.replies_text()
    assert "發言模式：gentle" in simulation.status_text()


def test_each_approach_can_be_stepped_through(config):
    rules = finish(Simulation.from_path(config, DEMO, "A", "gentle"))
    assert rules.engine.llm.stats.total_calls == 0 and "A 純規則" in rules.status_text()
    assert "純規則" in rules.trace_text() and rules.engine.replies
    single = finish(Simulation.from_path(config, DEMO, "B"))
    assert single.engine.llm.stats.total_calls == 20  # 21 則裡有 1 則是新成員入群
    assert "單一 LLM" in single.trace_text()


def test_reports_panel_has_the_rules_and_says_how_it_is_delivered(config):
    simulation = finish(Simulation.from_path(config, DEMO))
    text = simulation.reports_text()
    assert "m03" in text and "風險高" in text and "即時推播" in text and "規則：投資誘因或導流" in text


def test_trace_has_no_message_text_even_though_the_chat_does(config):
    simulation = finish(Simulation.from_path(config, DEMO))
    assert "保證獲利" in simulation.chat_text()
    assert "保證獲利" not in simulation.trace_text() and "stock-teacher" not in simulation.trace_text()
    assert "stock-teacher" not in simulation.reports_text() and "stock-teacher" not in simulation.watchdog_text()


def test_trace_lines_name_the_person_and_the_stage(config):
    simulation = finish(Simulation.from_path(config, DEMO))
    lines = format_trace(simulation.engine.trace, simulation.scenario)
    assert any(line.startswith("m03 08:05 大伯｜快速過濾｜") for line in lines)
    assert any("｜圖卡｜" in line for line in lines)


def test_watchdog_panel_shows_metrics_and_notices(config):
    strict = dataclasses.replace(config, watchdog=dataclasses.replace(config.watchdog, max_reminders_per_100=0,
                                                                     check_every_messages=1))
    simulation = finish(Simulation.from_path(strict, DEMO, speaking_mode="gentle"))
    text = simulation.watchdog_text()
    assert "最近 100 則主動提醒" in text and "本月推播" in text and "今天模型呼叫" in text
    assert "給守護人的通知" in text and "切回 silent" in text
    assert "發言模式：silent" in simulation.status_text()  # Watchdog 切回 silent 也看得到


def test_replay_without_a_cache_still_runs_to_the_end(config, tmp_path):
    replay = dataclasses.replace(config, llm_mode="replay", replay_cache_dir=tmp_path)
    simulation = finish(Simulation.from_path(replay, DEMO))
    assert simulation.finished and "查證未完成" in simulation.reports_text()
    assert "顧家貓現在沒辦法完成查證" in simulation.chat_text()


def test_every_scenario_in_the_folder_loads(config):
    for path in (PROJECT_ROOT / "scenarios").glob("*.yaml"):
        assert load_scenario(path).messages
