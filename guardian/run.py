"""命令列：用模擬劇本跑一次顧家貓引擎。

    python -m guardian.run scenarios/demo_family.yaml --mode offline
    python -m guardian.run scenarios/demo_family.yaml --mode offline --trace

輸出只有決策和統計，不印訊息原文。
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from .config import ConfigError, load_config, load_env_file
from .engine import DEMO_SALT, Engine, RunSummary
from .llm import MODES, LLMClient, LLMError, safe_error
from .models import BAND_LABELS, BANDS, KIND_LABELS, SchemaError
from .rules import FastFilter, RulesError
from .scenario import Scenario, ScenarioError, load_scenario

STAGE_LABELS = {"normalize": "正規化", "fast_path": "快速過濾"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m guardian.run", description="用模擬劇本跑一次顧家貓引擎")
    parser.add_argument("scenario", help="劇本檔，例如 scenarios/demo_family.yaml")
    parser.add_argument("--mode", choices=MODES, help="覆蓋 config.yaml 的 llm_mode")
    parser.add_argument("--config", default=None, help="設定檔路徑（預設是專案根目錄的 config.yaml）")
    parser.add_argument("--trace", action="store_true", help="逐則印出決策紀錄")
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
    print(f"模式：{config.llm_mode}　事件：{summary.events} 則　發言模式：{config.speaking_mode}")
    print(f"事件種類：{kinds}")
    print(f"快速過濾：{bands}（門檻：觀察 ≥ {config.thresholds.watch}，候選 ≥ {config.thresholds.candidate}）")
    print(f"候選佇列：{summary.candidates} 則　風險事件紀錄：{summary.records} 筆")
    print(f"模型呼叫：{llm['total_calls']} 次（{calls}）　錯誤：{llm['total_errors']} 次")
    if isinstance(engine.fast_filter, FastFilter):
        print(f"165 名單：{engine.fast_filter.blocklist.describe()}")
    print()
    print("※ Router 和 Worker 在 Phase 3 接上；目前候選和觀察事件只登記，不會呼叫模型。")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file()
    try:
        config = load_config(args.config)
        if args.mode:
            config = dataclasses.replace(config, llm_mode=args.mode)
        scenario = load_scenario(args.scenario)
        engine = Engine(config, salt=DEMO_SALT, llm_client=LLMClient.from_config(config))
        summary = engine.run(scenario.raw_events())
    except (ConfigError, ScenarioError, SchemaError, RulesError, LLMError, ValueError, OSError) as exc:
        print(f"錯誤：{safe_error(str(exc))}", file=sys.stderr)
        return 1
    if args.trace:
        print_trace(engine, scenario)
    print_summary(summary, scenario, engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
