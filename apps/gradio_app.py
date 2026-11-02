"""Gradio 模擬器與實驗區（SPEC 第 9 節）。只負責轉換輸入和輸出，邏輯都在 guardian/。

    python apps/gradio_app.py                # 預設 replay，不需要金鑰
    python apps/gradio_app.py --mode offline # 假模型，還沒錄製時先看流程
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gradio as gr  # noqa: E402
from PIL import Image  # noqa: E402

from guardian.approaches import APPROACH_LABELS, SIMULATOR_APPROACHES  # noqa: E402
from guardian.config import SPEAKING_MODES, Config, load_config, load_env_file  # noqa: E402
from guardian.experiment import compare_report, experiment_report, mode_notice, scenario_paths  # noqa: E402
from guardian.llm import MODES  # noqa: E402
from guardian.scenario import load_scenario  # noqa: E402
from guardian.simulation import Simulation  # noqa: E402

AUTO_SECONDS = 1.5
PANEL_LINES = 26
COLUMN_MIN_WIDTH = 280
EVENT_OPTIONS = {"api_visibility": "private", "concurrency_limit": 1}
SCROLL_PANELS = ("chat-panel", "trace-panel", "replies-panel", "reports-panel", "watchdog-panel")
# 每跑一則就捲到最新：這幾欄會一直變長，最新的內容在最下面
SCROLL_JS = """() => {
  for (const id of %s) {
    const box = document.querySelector(`#${id} textarea`);
    if (box) box.scrollTop = box.scrollHeight;
  }
}""" % list(SCROLL_PANELS)
LIVE_REFUSAL = "live 模式不在介面上跑實驗，避免按一下就花掉大量 token。請用 `python -m guardian.experiment --record`。"
INTRO = """# 顧家貓模擬器
左欄是家族群組，中欄是系統怎麼判斷，右欄是顧家貓的回覆、給守護人的報告和 Watchdog 指標。訊息都是模擬的。"""


def _card_image(card: bytes | None) -> Image.Image | None:
    return Image.open(io.BytesIO(card)) if card else None


class SimulatorHandlers:
    """介面事件的處理函式。Simulation 放在 gr.State，每位使用者一份。"""

    def __init__(self, config: Config) -> None:
        self.config = config

    @staticmethod
    def _panels(sim: Simulation) -> tuple[Any, ...]:
        return (sim, sim.status_text(), sim.chat_text(), sim.trace_text(), sim.replies_text(),
                _card_image(sim.latest_card()), sim.reports_text(), sim.watchdog_text(), sim.engine.speaking_mode)

    @staticmethod
    def _auto(running: bool) -> tuple[Any, ...]:
        return running, gr.Timer(active=running), gr.Button(value="暫停" if running else "自動播放")

    def restart(self, path: str, approach: str, mode: str) -> tuple[Any, ...]:
        sim = Simulation.from_path(self.config, path, approach, mode)
        return (*self._panels(sim), *self._auto(False))

    def next_step(self, sim: Simulation | None) -> Any:
        if sim is None:
            return gr.skip()
        sim.step()
        return self._panels(sim)

    def change_mode(self, sim: Simulation | None, mode: str) -> Any:
        """發言模式可以隨時切換，不用重跑（守護人也是隨時下指令）。"""
        if sim is None:
            return gr.skip()
        sim.engine.speaking_mode = mode
        return self._panels(sim)

    def toggle_auto(self, running: bool, sim: Simulation | None) -> tuple[Any, ...]:
        return self._auto(not running and sim is not None and not sim.finished)

    def tick(self, sim: Simulation | None) -> Any:
        if sim is None:
            return gr.skip()
        more = sim.step()
        return (*self._panels(sim), *self._auto(more))

    def report(self, builder: Callable[[Config], str]) -> str:
        return LIVE_REFUSAL if self.config.llm_mode == "live" else builder(self.config)


def build_demo(config: Config) -> gr.Blocks:
    handlers = SimulatorHandlers(config)
    paths = scenario_paths(config)
    scenarios = [(load_scenario(path).name, str(path)) for path in paths]

    with gr.Blocks(title="顧家貓模擬器", analytics_enabled=False) as demo:
        gr.Markdown(INTRO)
        gr.Markdown(mode_notice(config))
        with gr.Tab("模擬器"):
            with gr.Row():
                scenario_box = gr.Dropdown(scenarios, value=scenarios[0][1], label="劇本", filterable=False)
                mode_box = gr.Dropdown(list(SPEAKING_MODES), value=config.speaking_mode, label="發言模式",
                                       filterable=False)
                approach_box = gr.Dropdown([(APPROACH_LABELS[key], key) for key in SIMULATOR_APPROACHES],
                                           value=SIMULATOR_APPROACHES[0], label="架構", filterable=False)
            with gr.Row():
                step_button = gr.Button("下一步", variant="primary")
                auto_button = gr.Button("自動播放")
                reset_button = gr.Button("重新開始")
            status = gr.Textbox(label="狀態", interactive=False, lines=1)
            with gr.Row():
                with gr.Column(min_width=COLUMN_MIN_WIDTH):
                    chat = gr.Textbox(label="家族群組", interactive=False, lines=PANEL_LINES, max_lines=PANEL_LINES,
                                      autoscroll=False, elem_id="chat-panel")
                with gr.Column(min_width=COLUMN_MIN_WIDTH):
                    trace = gr.Textbox(label="決策紀錄", interactive=False, lines=PANEL_LINES, max_lines=PANEL_LINES,
                                       autoscroll=False, elem_id="trace-panel")
                with gr.Column(min_width=COLUMN_MIN_WIDTH):
                    replies = gr.Textbox(label="群組回覆", interactive=False, lines=4, max_lines=4,
                                         autoscroll=False, elem_id="replies-panel")
                    card = gr.Image(label="警示圖卡", interactive=False, type="pil", format="png", height=280,
                                    sources=[], buttons=[])
                    reports = gr.Textbox(label="守護人報告", interactive=False, lines=8, max_lines=8,
                                         autoscroll=False, elem_id="reports-panel")
                    watchdog = gr.Textbox(label="Watchdog 指標", interactive=False, lines=8, max_lines=8,
                                          autoscroll=False, elem_id="watchdog-panel")

        with gr.Tab("實驗區"):
            gr.Markdown("三種做法（A 純規則、B 單一 LLM、C Super-agent）、消融實驗和模型比較，指標都由程式計算（SPEC 11-1）。")
            with gr.Row():
                experiment_button = gr.Button("跑三種做法和消融實驗", variant="primary")
                compare_button = gr.Button("跑模型比較")
            result = gr.Markdown()

        sim_state = gr.State(None)
        auto_state = gr.State(False)
        timer = gr.Timer(AUTO_SECONDS, active=False)
        panels = [sim_state, status, chat, trace, replies, card, reports, watchdog, mode_box]
        auto_parts = [auto_state, timer, auto_button]
        controls = [scenario_box, approach_box, mode_box]

        demo.load(handlers.restart, controls, panels + auto_parts, **EVENT_OPTIONS)
        reset_button.click(handlers.restart, controls, panels + auto_parts, **EVENT_OPTIONS)
        # 用 input 不用 change：Watchdog 切回 silent 時程式也會更新下拉選單，不能再觸發一次
        scenario_box.input(handlers.restart, controls, panels + auto_parts, **EVENT_OPTIONS)
        approach_box.input(handlers.restart, controls, panels + auto_parts, **EVENT_OPTIONS)
        mode_box.input(handlers.change_mode, [sim_state, mode_box], panels, **EVENT_OPTIONS)
        step_button.click(handlers.next_step, [sim_state], panels, **EVENT_OPTIONS).then(
            None, None, None, js=SCROLL_JS, api_visibility="private")
        auto_button.click(handlers.toggle_auto, [auto_state, sim_state], auto_parts, **EVENT_OPTIONS)
        timer.tick(handlers.tick, [sim_state], panels + auto_parts, show_progress="hidden", **EVENT_OPTIONS).then(
            None, None, None, js=SCROLL_JS, api_visibility="private")
        experiment_button.click(lambda: handlers.report(experiment_report), None, result, **EVENT_OPTIONS)
        compare_button.click(lambda: handlers.report(compare_report), None, result, **EVENT_OPTIONS)
    return demo


_current_demo: gr.Blocks | None = None


def launch(config: Config, **launch_options: Any) -> gr.Blocks:
    """重建介面前先關掉舊的（notebook 重跑這一格時）。"""
    global _current_demo
    if _current_demo is not None:
        _current_demo.close()
    _current_demo = build_demo(config)
    _current_demo.queue(default_concurrency_limit=1, api_open=False)
    _current_demo.launch(footer_links=["gradio", "settings"], **launch_options)
    return _current_demo


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python apps/gradio_app.py", description="顧家貓模擬器與實驗區")
    parser.add_argument("--mode", choices=MODES, help="覆蓋 config.yaml 的 llm_mode")
    parser.add_argument("--port", type=int, default=None, help="連接埠")
    parser.add_argument("--share", action="store_true", help="產生公開網址（Colab 用）")
    parser.add_argument("--config", default=None, help="設定檔路徑")
    args = parser.parse_args(argv)
    load_env_file()
    config = load_config(args.config)
    if args.mode:
        config = dataclasses.replace(config, llm_mode=args.mode)
    launch(config, server_port=args.port, share=args.share)


if __name__ == "__main__":
    main()
