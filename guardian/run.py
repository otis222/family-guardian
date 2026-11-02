"""命令列：用模擬劇本跑一次顧家貓引擎。

    python -m guardian.run scenarios/demo_family.yaml --mode offline
    python -m guardian.run scenarios/demo_family.yaml --mode offline --trace
    python -m guardian.run scenarios/demo_family.yaml --mode offline --daily-summary

輸出只有決策和統計，不印訊息原文。
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from .config import SPEAKING_MODES, ConfigError, load_config, load_env_file
from .engine import DEMO_SALT, Engine, RunSummary
from .llm import MODES, LLMClient, LLMError, safe_error
from .models import BAND_LABELS, BANDS, KIND_LABELS, RISK_LABELS, SchemaError
from .rules import FastFilter, RulesError
from .scenario import Scenario, ScenarioError, load_scenario

STAGE_LABELS = {
    "normalize": "正規化",
    "fast_path": "快速過濾",
    "thread": "Thread",
    "router": "Router",
    "verifier": "查證",
    "early_action": "提前行動",
    "reflection": "發言前自檢",
    "card": "圖卡",
    "speak": "發言",
    "reporter": "回報",
    "watchdog": "Watchdog",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m guardian.run", description="用模擬劇本跑一次顧家貓引擎")
    parser.add_argument("scenario", help="劇本檔，例如 scenarios/demo_family.yaml")
    parser.add_argument("--mode", choices=MODES, help="覆蓋 config.yaml 的 llm_mode")
    parser.add_argument("--speaking", choices=SPEAKING_MODES, help="覆蓋 config.yaml 的發言模式")
    parser.add_argument("--config", default=None, help="設定檔路徑（預設是專案根目錄的 config.yaml）")
    parser.add_argument("--trace", action="store_true", help="逐則印出決策紀錄")
    parser.add_argument("--daily-summary", action="store_true", help="最後印出給守護人的今日摘要")
    return parser


def print_trace(engine: Engine, scenario: Scenario) -> None:
    messages = {message.id: message for message in scenario.messages}
    print("決策紀錄")
    for entry in engine.trace:
        message = messages.get(entry.event_id)
        when = message.time.strftime("%H:%M") if message else "--:--"
        who = scenario.member_name(message.sender) if message else "?"
        print(f"  {entry.event_id} {when} {who}｜{STAGE_LABELS.get(entry.stage, entry.stage)}｜{entry.message}")
    print()


def print_summary(summary: RunSummary, scenario: Scenario, engine: Engine) -> None:
    config = engine.config
    llm = summary.llm
    kinds = "｜".join(f"{KIND_LABELS.get(kind, kind)} {count}" for kind, count in summary.by_kind.items())
    bands = "｜".join(f"{BAND_LABELS[band]} {summary.by_band[band]}" for band in BANDS)
    calls = "｜".join(f"{role} {count}" for role, count in llm["calls"].items())
    print(f"顧家貓 · {scenario.name}")
    print(f"模式：{config.llm_mode}　事件：{summary.events} 則　發言模式：{engine.speaking_mode}")
    print(f"事件種類：{kinds}")
    print(f"快速過濾：{bands}（門檻：觀察 ≥ {config.thresholds.watch}，候選 ≥ {config.thresholds.candidate}）")
    print(f"候選佇列：進過 {summary.candidates} 則，還留 {summary.pending} 則，Router 跑 {summary.router_rounds} 輪")
    print(f"風險事件紀錄：{summary.records} 筆　守護人報告：{summary.reports} 則　群組回覆：{summary.replies} 則")
    print(f"模型呼叫：{llm['total_calls']} 次（{calls}）　錯誤：{llm['total_errors']} 次")
    if isinstance(engine.fast_filter, FastFilter):
        print(f"165 名單：{engine.fast_filter.blocklist.describe()}")
    print(f"165 闢謠專區：{engine.verifier.rumors.describe()}")
    watch = engine.watchdog.snapshot()
    state = f"模型暫停（{watch['pause_reason']}）" if watch["llm_paused"] else "模型正常"
    print(f"Watchdog：檢查 {watch['checks']} 次｜通知 {summary.notices} 則｜{state}"
          f"｜本月推播 {watch['push_used']}／{watch['push_quota']} 則")
    for report in engine.reports:
        level = RISK_LABELS.get(report.risk_level, "查證未完成")
        print(f"報告 {report.event_id}：{level}{'（即時）' if report.push else ''}｜{'、'.join(report.rules)}")
    for reply in engine.replies:
        print(f"回覆 {reply.event_id}（{reply.source}{'＋圖卡' if reply.card else ''}）：{reply.text}")
    for notice in engine.notices:
        print(f"通知（{notice.time:%H:%M}）：{notice.message}")


def _never_crash_on_encoding() -> None:
    """轉存檔案時用的是系統編碼（例如 cp950），模型文字裡的符號可能編不出來；換成問號，不要整個中斷。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _never_crash_on_encoding()
    args = build_parser().parse_args(argv)
    load_env_file()
    try:
        config = load_config(args.config)
        if args.mode:
            config = dataclasses.replace(config, llm_mode=args.mode)
        if args.speaking:
            config = dataclasses.replace(config, speaking_mode=args.speaking)
        scenario = load_scenario(args.scenario)
        engine = Engine(config, salt=DEMO_SALT, llm_client=LLMClient.from_config(config))
        summary = engine.run(scenario.raw_events())
    except (ConfigError, ScenarioError, SchemaError, RulesError, LLMError, ValueError, OSError) as exc:
        print(f"錯誤：{safe_error(str(exc))}", file=sys.stderr)
        return 1
    if args.trace:
        print_trace(engine, scenario)
    print_summary(summary, scenario, engine)
    if args.daily_summary:
        print()
        print(engine.daily_summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
