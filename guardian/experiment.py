"""實驗（SPEC 11-1）：三種做法、消融實驗和模型比較，指標都由程式算。

    python -m guardian.experiment --mode offline   # 假模型，只驗證流程
    python -m guardian.experiment                  # replay：用錄好的回應重現結果，不需要金鑰
    python -m guardian.experiment --record         # 用真的模型錄製，需要 GROQ_API_KEY

指標的定義（決策 D33）：
- 發出警示：守護人收到 medium 以上的報告，或顧家貓在群組發出提醒。「查證未完成」不算。
- 召回率只算 scam；精確率的分子含 injection，因為它們都夾帶詐騙內容，提醒是對的。
- 打擾：主動提醒的次數，被 @ 的回應不算。
- 延遲：詐騙訊息之後又過了幾則訊息才發出警示，0 代表當下就發出。
- 注入成功：injection 訊息沒有發出警示。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .approaches import APPROACH_LABELS, build_engine
from .config import Config, ConfigError, ModelCandidate, load_config, load_env_file
from .engine import Engine
from .fileio import write_text_atomic
from .llm import (
    MODES,
    ROLES,
    LLMClient,
    LLMError,
    LLMRecordedFailureError,
    LLMUnusableReplyError,
    cache_key,
    safe_error,
)
from .router import NEUTRAL_TEMPLATE, UNVERIFIED_TEMPLATE
from .rules import RulesError
from .scenario import Scenario, ScenarioError, load_scenario

RUN_KEYS = ("A", "B", "C", "C-no-filter", "C-no-early", "C-no-batch")
ALERT_LEVELS = ("medium", "high")
NOT_ALERTS = (NEUTRAL_TEMPLATE, UNVERIFIED_TEMPLATE)
UNLIMITED = 10 ** 12
UNUSABLE_REPLY_ATTEMPTS = 3  # 錄製時，同一個 prompt 回覆不能用最多問幾次（決策 D53）
EXPERIMENT_NOTE = (
    "跑實驗時發言模式固定 gentle，並放寬 Watchdog 的上限：實驗資料的詐騙密度遠高於真實群組，不放寬會中途切回 silent 或暫停模型。\n"
    "警示＝守護人收到 medium 以上的報告，或群組提醒。召回率只算 scam；精確率的分子含 injection（都夾帶詐騙內容）。"
    "打擾只算主動提醒。延遲是詐騙之後過了幾則才警示。注入成功＝injection 訊息沒有發出警示。"
)

ClientFactory = Callable[..., LLMClient]


@dataclass
class RunResult:
    key: str
    label: str
    messages: int = 0
    scam_total: int = 0
    scam_flagged: int = 0
    injection_total: int = 0
    injection_flagged: int = 0
    normal_total: int = 0
    normal_flagged: int = 0
    interruptions: int = 0
    latencies: list[int] = field(default_factory=list)
    llm_calls: int = 0
    tokens: int = 0
    errors: int = 0
    replay_misses: int = 0
    missed_scams: list[str] = field(default_factory=list)
    missed_injections: list[str] = field(default_factory=list)
    false_alarms: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        return self.scam_flagged / self.scam_total if self.scam_total else None

    @property
    def precision(self) -> float | None:
        hits = self.scam_flagged + self.injection_flagged
        return hits / (hits + self.normal_flagged) if hits + self.normal_flagged else None

    @property
    def accuracy(self) -> float | None:
        correct = self.scam_flagged + self.injection_flagged + self.normal_total - self.normal_flagged
        return correct / self.messages if self.messages else None

    @property
    def interruptions_per_100(self) -> float:
        return self.interruptions / self.messages * 100 if self.messages else 0.0

    @property
    def mean_latency(self) -> float | None:
        return sum(self.latencies) / len(self.latencies) if self.latencies else None

    @property
    def injection_success_rate(self) -> float | None:
        return (self.injection_total - self.injection_flagged) / self.injection_total if self.injection_total else None


def experiment_config(config: Config) -> Config:
    watchdog = dataclasses.replace(config.watchdog, max_reminders_per_100=UNLIMITED, max_llm_calls_per_day=UNLIMITED,
                                   max_tokens_per_day=UNLIMITED, max_error_rate=1.0)
    return dataclasses.replace(config, speaking_mode="gentle", watchdog=watchdog)


def evaluate(engine: Engine, scenario: Scenario, key: str) -> RunResult:
    """一則一則跑，記下每則訊息第一次發出警示時跑到第幾則。"""
    alert_at: dict[str, int] = {}
    reports_seen = replies_seen = 0

    def collect(index: int) -> None:
        nonlocal reports_seen, replies_seen
        for report in engine.reports[reports_seen:]:
            if report.risk_level in ALERT_LEVELS:
                alert_at.setdefault(report.event_id, index)
        for reply in engine.replies[replies_seen:]:
            if reply.text not in NOT_ALERTS:
                alert_at.setdefault(reply.event_id, index)
        reports_seen, replies_seen = len(engine.reports), len(engine.replies)

    messages = scenario.messages
    for index, message in enumerate(messages):
        engine.process(message.to_raw_event(scenario.group_id))
        collect(index)
    engine.flush()
    collect(len(messages) - 1)

    mentions = {message.id for message in messages if message.mention}
    stats = engine.llm.stats
    result = RunResult(
        key, APPROACH_LABELS.get(key, key),
        interruptions=sum(1 for reply in engine.replies if reply.event_id not in mentions),
        llm_calls=stats.total_calls, tokens=stats.prompt_tokens + stats.completion_tokens, errors=stats.total_errors,
        replay_misses=stats.replay_misses,
    )
    for index, message in enumerate(messages):
        if message.kind != "text":
            continue
        result.messages += 1
        flagged = message.id in alert_at
        if message.label == "scam":
            result.scam_total += 1
            result.scam_flagged += flagged
            if flagged:
                result.latencies.append(alert_at[message.id] - index)
            else:
                result.missed_scams.append(message.id)
        elif message.label == "injection":
            result.injection_total += 1
            result.injection_flagged += flagged
            if not flagged:
                result.missed_injections.append(message.id)
        else:
            result.normal_total += 1
            result.normal_flagged += flagged
            if flagged:
                result.false_alarms.append(message.id)
    return result


def _default_factory(settings: Config) -> ClientFactory:
    return lambda **overrides: LLMClient.from_config(settings, **overrides)


def run_experiment(config: Config, scenario: Scenario, make_client: ClientFactory | None = None,
                   keys: tuple[str, ...] = RUN_KEYS) -> list[RunResult]:
    settings = experiment_config(config)
    make = make_client or _default_factory(settings)
    return [evaluate(build_engine(key, settings, scenario, llm_client=make()), scenario, key) for key in keys]


# ---------------------------------------------------------------- 模型比較

@dataclass
class CompareRow:
    role: str
    model: str
    result: RunResult


def _candidate_params(config: Config, role: str, candidate: ModelCandidate) -> dict[str, dict[str, Any]]:
    params = {name: config.params_for(name) for name in ROLES}
    merged = {**params[role], **candidate.params}
    params[role] = {key: value for key, value in merged.items() if value is not None}
    return params


def compare_models(config: Config, scenario: Scenario, make_client: ClientFactory | None = None) -> list[CompareRow]:
    """一次只換一個角色的模型，其他維持 config.yaml 的設定，用同一批訊息比較（決策 D34）。"""
    settings = experiment_config(config)
    make = make_client or _default_factory(settings)
    subset = scenario.compare_subset()
    rows = []
    for role, candidates in config.experiment.compare.items():
        for candidate in candidates:
            client = make(models={**config.models, role: candidate.model},
                          params=_candidate_params(config, role, candidate))
            engine = build_engine("C", settings, subset, llm_client=client)
            rows.append(CompareRow(role, candidate.model, evaluate(engine, subset, "C")))
    return rows


# ---------------------------------------------------------------- 表格

def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _number(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def format_results(results: list[RunResult]) -> str:
    return _table(
        ["做法", "召回率", "精確率", "打擾／100 則", "平均延遲（則）", "模型呼叫", "token", "注入成功率", "錯誤"],
        [[r.label, _percent(r.recall), _percent(r.precision), _number(r.interruptions_per_100),
          _number(r.mean_latency), str(r.llm_calls), str(r.tokens), _percent(r.injection_success_rate), str(r.errors)]
         for r in results],
    )


def format_details(results: list[RunResult]) -> str:
    """漏掉和誤報的事件編號，給錯誤分析用。"""
    lines = []
    for r in results:
        lines.append(f"- {r.label}：漏掉詐騙 {'、'.join(r.missed_scams) or '無'}；"
                     f"注入成功 {'、'.join(r.missed_injections) or '無'}；誤報 {'、'.join(r.false_alarms) or '無'}")
    return "\n".join(lines)


def format_compare(rows: list[CompareRow]) -> str:
    return _table(
        ["角色", "模型", "準確率", "召回率", "精確率", "模型呼叫", "token", "錯誤"],
        [[row.role, row.model, _percent(row.result.accuracy), _percent(row.result.recall),
          _percent(row.result.precision), str(row.result.llm_calls), str(row.result.tokens), str(row.result.errors)]
         for row in rows],
    )


def mode_notice(config: Config) -> str:
    """提醒現在看到的結果是什麼來源。"""
    if config.llm_mode == "offline":
        return "目前是 offline 假模型：結果只驗證流程，不是真實的判斷。"
    if config.llm_mode == "live":
        return "目前是 live：每次判斷都會呼叫模型、花 token。"
    count = sum(1 for _ in config.replay_cache_dir.glob("*/*.json"))
    if not count:
        return ("目前是 replay，但回放快取還沒錄製：模型呼叫會全部失敗，只看得到規則結果。"
                "錄製用 python -m guardian.experiment --record；想先看流程可以加 --mode offline。")
    return f"目前是 replay：用錄好的 {count} 筆回應重現結果，不需要金鑰。"


def _error_notice(results: list[RunResult]) -> str:
    misses = sum(result.replay_misses for result in results)
    if misses:
        return f"\n\n**有 {misses} 次回放快取找不到，還沒錄完，上面的數字不能當結果。**"
    errors = sum(result.errors for result in results)
    if errors:
        return f"\n\n錯誤欄：模型有 {errors} 次給不出能用的回覆，這些事件照查證失敗的路徑處理，已經反映在數字裡。"
    return ""


def experiment_report(config: Config, make_client: ClientFactory | None = None) -> str:
    scenario = load_scenario(config.experiment.scenario)
    results = run_experiment(config, scenario, make_client)
    counts = "、".join(f"{label} {count}" for label, count in scenario.label_counts().items())
    parts = [f"### 三種做法和消融實驗（{scenario.name}：{len(scenario.messages)} 則，{counts}）",
             mode_notice(config), format_results(results), EXPERIMENT_NOTE,
             "#### 漏掉和誤報的事件", format_details(results)]
    return "\n\n".join(parts) + _error_notice(results)


def compare_report(config: Config, make_client: ClientFactory | None = None) -> str:
    scenario = load_scenario(config.experiment.scenario)
    rows = compare_models(config, scenario, make_client)
    parts = [f"### 模型比較（{sum(m.compare for m in scenario.messages)} 則）", mode_notice(config),
             format_compare(rows), "一次只換一個角色的模型，其他維持 config.yaml 的設定。"]
    return "\n\n".join(parts) + _error_notice([row.result for row in rows])


# ---------------------------------------------------------------- 錄製

class RecordingStopped(RuntimeError):
    """錄製時模型呼叫失敗。不是 LLMError，引擎不會把它當成一般失敗繼續跑下去，免得錄進本來不會出現的 prompt。"""


class RecordingLLM(LLMClient):
    """錄製用（決策 D36、D53）：已經錄過的 prompt 直接讀快取，不再花 token；失敗就停，再跑一次會接著錄。

    只有模型給不出能用的回覆是例外（空白、想呼叫工具）：最多問幾次，還是不行就把這個結果記下來繼續錄，
    回放時會重現成同樣的失敗。這是模型本身的表現，模型比較要看得到，也不該讓整個錄製停住。
    """

    def __init__(self, mode: str, models: Mapping[str, str], *, pace_seconds: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep, **kwargs: Any) -> None:
        super().__init__(mode, models, **kwargs)
        self.pace_seconds = pace_seconds
        self._sleep = sleep
        self.recorded = 0
        self.reused = 0
        self.unusable_replies = 0
        self.gave_up = 0
        self.new_tokens = 0  # 這次真的花掉的；stats 裡的 token 含沿用快取的

    def call(self, role: str, system_prompt: str, user_prompt: str) -> str:
        for attempt in range(UNUSABLE_REPLY_ATTEMPTS):
            try:
                return super().call(role, system_prompt, user_prompt)
            except LLMRecordedFailureError:
                raise
            except LLMUnusableReplyError:
                self.unusable_replies += 1
                if attempt == UNUSABLE_REPLY_ATTEMPTS - 1:
                    self.gave_up += 1
                    self._write_unusable(role, system_prompt, user_prompt)
                    raise
            except LLMError as exc:
                raise RecordingStopped(str(exc)) from None
        raise AssertionError("不會走到這裡")

    def _write_unusable(self, role: str, system_prompt: str, user_prompt: str) -> None:
        model = self.models[role]
        key = cache_key(role, model, system_prompt, user_prompt)
        path = self._cache_path(role, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps({"key": key, "role": role, "model": model, "unusable": True},
                                           ensure_ascii=False, indent=2) + "\n")

    def _live(self, role: str, model: str, system_prompt: str, user_prompt: str) -> str:
        if self._cache_path(role, cache_key(role, model, system_prompt, user_prompt)).is_file():
            self.reused += 1
            return self._replay(role, model, system_prompt, user_prompt)
        self._sleep(self.pace_seconds)  # 避免超過供應商的每分鐘上限；失敗時不自動重試
        before = self.stats.prompt_tokens + self.stats.completion_tokens
        text = super()._live(role, model, system_prompt, user_prompt)
        self.new_tokens += self.stats.prompt_tokens + self.stats.completion_tokens - before
        self.recorded += 1
        return text


def scenario_paths(config: Config) -> list[Path]:
    """劇本資料夾裡的所有劇本，示範劇本排最前面。"""
    folder = config.experiment.scenario.parent
    return sorted(folder.glob("*.yaml"), key=lambda path: (path != folder / "demo_family.yaml", path.name))


def freeze_rumors(config: Config) -> str:
    """把目前下載的闢謠專區凍結給模擬和實驗用：prompt 會帶檢索結果，資料一變回放就對不上（決策 D35）。"""
    name = f"{config.data_165.rumors.id}.csv"
    target = config.experiment.snapshot_dir / name
    if target.is_file():
        return "沿用已經凍結的闢謠資料。"
    source = config.data_165.cache_dir / name
    if not source.is_file():
        return ("還沒下載闢謠專區，這次錄製的查證不會有 search_165_rumors。"
                "想要的話先跑 python -m guardian.data_165 --update，再清掉 replay_cache 重錄。")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return f"已凍結闢謠資料：{target.name}"


def record_all(config: Config, *, log: Callable[[str], None] = print, **client_options: Any) -> dict[str, int]:
    """錄下模擬器和實驗會用到的所有呼叫。發言模式用 gentle：silent 和 on_mention 的呼叫都是它的子集。"""
    log(freeze_rumors(config))
    settings = experiment_config(config)
    totals = {"recorded": 0, "reused": 0, "tokens": 0, "unusable_replies": 0, "gave_up": 0}
    clients: list[RecordingLLM] = []

    def make(**overrides: Any) -> RecordingLLM:
        client = RecordingLLM.from_config(settings, mode="live", write_cache=True,
                                          pace_seconds=config.experiment.pace_seconds,
                                          **{**client_options, **overrides})
        clients.append(client)
        return client

    def settle(name: str) -> None:
        recorded = sum(client.recorded for client in clients)
        reused = sum(client.reused for client in clients)
        tokens = sum(client.new_tokens for client in clients)
        unusable = sum(client.unusable_replies for client in clients)
        gave_up = sum(client.gave_up for client in clients)
        log(f"{name}：新錄 {recorded} 次（{tokens} token），沿用 {reused} 次，回覆不能用 {unusable} 次，放棄 {gave_up} 次")
        totals["recorded"] += recorded
        totals["reused"] += reused
        totals["tokens"] += tokens
        totals["unusable_replies"] += unusable
        totals["gave_up"] += gave_up
        clients.clear()

    try:
        for path in scenario_paths(config):
            scenario = load_scenario(path)
            for key in ("C", "B"):
                evaluate(build_engine(key, settings, scenario, llm_client=make()), scenario, key)
                settle(f"{scenario.name}／{APPROACH_LABELS[key]}")
        experiment = load_scenario(config.experiment.scenario)
        run_experiment(config, experiment, make)
        settle("消融實驗")
        compare_models(config, experiment, make)
        settle("模型比較")
    except RecordingStopped:
        settle("中斷前")
        raise
    return totals


# ---------------------------------------------------------------- 命令列

def main(argv: list[str] | None = None) -> int:
    from .run import never_crash_on_encoding

    never_crash_on_encoding()
    parser = argparse.ArgumentParser(prog="python -m guardian.experiment", description="三種做法、消融實驗和模型比較")
    parser.add_argument("--mode", choices=MODES, help="覆蓋 config.yaml 的 llm_mode")
    parser.add_argument("--record", action="store_true", help="用真的模型錄製回放快取（需要金鑰）；已錄過的不再花 token")
    parser.add_argument("--no-compare", action="store_true", help="不跑模型比較")
    parser.add_argument("--output", help="把結果另存成這個檔案（UTF-8）")
    parser.add_argument("--config", default=None, help="設定檔路徑")
    args = parser.parse_args(argv)
    load_env_file()
    try:
        config = load_config(args.config)
        if args.mode:
            config = dataclasses.replace(config, llm_mode=args.mode)
        if args.record:
            totals = record_all(config, log=lambda line: print(line, flush=True))
            print(f"錄製完成：這次新錄 {totals['recorded']} 次，花了 {totals['tokens']} token，"
                  f"回覆不能用 {totals['unusable_replies']} 次（重問後仍不行 {totals['gave_up']} 次）。"
                  "以下是用回放重現的結果。\n")
            config = dataclasses.replace(config, llm_mode="replay")
        report = experiment_report(config)
        if not args.no_compare:
            report += "\n\n" + compare_report(config)
    except RecordingStopped as exc:
        print(f"錄製中斷：{safe_error(str(exc))}", file=sys.stderr)
        print("已錄好的不用重花 token，處理好之後再跑一次 --record 會接著錄。", file=sys.stderr)
        return 1
    except (ConfigError, ScenarioError, RulesError, ValueError, OSError) as exc:
        print(f"錯誤：{safe_error(str(exc))}", file=sys.stderr)
        return 1
    print(report)
    if args.output:
        Path(args.output).write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
