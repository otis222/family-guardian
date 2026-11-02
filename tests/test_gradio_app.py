"""Gradio 介面（SPEC 第 9 節）：設定比照規格，事件處理函式正確。不啟動伺服器。"""

import dataclasses

import gradio as gr
import pytest

from apps import gradio_app
from apps.gradio_app import SimulatorHandlers, build_demo
from guardian.config import load_config
from guardian.experiment import scenario_paths


@pytest.fixture
def config():
    return dataclasses.replace(load_config(), llm_mode="offline")


@pytest.fixture
def handlers(config):
    return SimulatorHandlers(config)


@pytest.fixture
def demo_path(config):
    return str(scenario_paths(config)[0])


PANELS = 9  # sim、狀態、群組、決策紀錄、回覆、圖卡、報告、Watchdog、發言模式


def started(handlers, demo_path, approach="C", mode="silent"):
    return handlers.restart(demo_path, approach, mode)[0]


# ---------------------------------------------------------------- 規格要求的設定

def test_every_event_is_private_and_runs_one_at_a_time(config):
    demo = build_demo(config)
    assert demo.fns
    for fn in demo.fns.values():
        assert fn.api_visibility == "private", fn.name
        if fn.fn is not None:  # 只跑前端 JS 的事件不佔佇列
            assert fn.concurrency_limit == 1, fn.name


def test_only_plain_text_components_besides_the_card(config):
    # 訊息內容不能被當成 HTML 算繪；規格要求純文字元件
    blocks = list(build_demo(config).blocks.values())
    assert not [b for b in blocks if isinstance(b, (gr.HTML, gr.Chatbot, gr.File, gr.Dataframe))]
    assert len([b for b in blocks if isinstance(b, gr.Image)]) == 1
    assert [b.label for b in blocks if isinstance(b, gr.Dropdown)] == ["劇本", "發言模式", "架構"]


def test_message_panels_cannot_be_edited(config):
    for block in build_demo(config).blocks.values():
        if isinstance(block, gr.Textbox):
            assert block.interactive is False, block.label


def test_page_says_where_results_come_from(config):
    replay = dataclasses.replace(config, llm_mode="replay", replay_cache_dir=config.replay_cache_dir / "nothing-here")
    markdown = [b.value for b in build_demo(replay).blocks.values() if isinstance(b, gr.Markdown)]
    assert any("還沒錄製" in (text or "") for text in markdown)


def test_launch_closes_the_previous_demo_and_turns_the_public_api_off(config, monkeypatch):
    closed, launched, queued = [], [], []
    monkeypatch.setattr(gr.Blocks, "close", lambda self: closed.append(self))
    monkeypatch.setattr(gr.Blocks, "launch", lambda self, **options: launched.append(options))
    original_queue = gr.Blocks.queue
    monkeypatch.setattr(gr.Blocks, "queue", lambda self, **options: (queued.append(options),
                                                                     original_queue(self, **options))[1])
    monkeypatch.setattr(gradio_app, "_current_demo", None)
    first = gradio_app.launch(config, server_port=1)
    second = gradio_app.launch(config, server_port=2)
    assert closed == [first] and second is not first
    assert {"default_concurrency_limit": 1, "api_open": False} in queued
    assert "api" not in launched[0]["footer_links"]


# ---------------------------------------------------------------- 事件處理

def test_restart_fills_the_panels_and_stops_auto_play(handlers, demo_path):
    result = handlers.restart(demo_path, "C", "silent")
    assert len(result) == PANELS + 3
    sim, status, chat = result[:3]
    assert sim.position == 0 and "第 0／21 則" in status and chat == ""
    running, timer, button = result[PANELS:]
    assert running is False and timer.active is False and button.value == "自動播放"


def test_next_step_advances_the_same_simulation(handlers, demo_path):
    sim = started(handlers, demo_path)
    result = handlers.next_step(sim)
    assert len(result) == PANELS and result[0] is sim and sim.position == 1
    assert "阿嬤" in result[2] and "快速過濾" in result[3]


def test_card_panel_gets_an_image_once_there_is_a_warning(handlers, demo_path):
    sim = started(handlers, demo_path)
    assert handlers.next_step(sim)[5] is None
    while sim.step():
        pass
    image = handlers.next_step(sim)[5]
    assert image.size == (1040, 1040)


def test_mode_change_does_not_restart(handlers, demo_path):
    sim = started(handlers, demo_path)
    handlers.next_step(sim)
    result = handlers.change_mode(sim, "gentle")
    assert sim.position == 1 and result[8] == "gentle" and "發言模式：gentle" in result[1]


def test_changing_the_architecture_restarts_with_a_new_engine(handlers, demo_path):
    first = started(handlers, demo_path, "C")
    handlers.next_step(first)
    second = started(handlers, demo_path, "A")
    assert second is not first and second.position == 0 and second.approach == "A"


def test_auto_play_toggles_and_stops_by_itself(handlers, demo_path):
    sim = started(handlers, demo_path)
    running, timer, button = handlers.toggle_auto(False, sim)
    assert running is True and timer.active is True and button.value == "暫停"
    assert handlers.toggle_auto(True, sim)[0] is False

    result = handlers.tick(sim)
    assert sim.position == 1 and result[PANELS] is True
    while not sim.finished:
        result = handlers.tick(sim)
    assert result[PANELS] is False and result[PANELS + 1].active is False


def test_auto_play_does_not_start_after_the_end(handlers, demo_path):
    sim = started(handlers, demo_path)
    while sim.step():
        pass
    assert handlers.toggle_auto(False, sim)[0] is False


def test_events_before_the_page_is_ready_do_nothing(handlers):
    assert handlers.next_step(None) == gr.skip()
    assert handlers.tick(None) == gr.skip()
    assert handlers.change_mode(None, "gentle") == gr.skip()
    assert handlers.toggle_auto(False, None)[0] is False


def test_experiment_buttons_show_reports(handlers):
    assert "| C Super-agent |" in handlers.report(gradio_app.experiment_report)


def test_live_mode_refuses_to_run_the_experiment_from_a_button(config):
    live = SimulatorHandlers(dataclasses.replace(config, llm_mode="live"))

    def explode(_):
        raise AssertionError("不該花 token")

    assert "不在介面上跑實驗" in live.report(explode)
