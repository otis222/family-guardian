"""實驗：指標、消融實驗、模型比較、錄製和回放（SPEC 11-1、第 13 節第 12 項）。"""

import dataclasses

import pytest
from helpers import RecordingAisuite, Scripted, fixed_filter, make_engine, message, route_all, scenario_of, verify_json

from guardian import experiment
from guardian.approaches import SIMULATOR_APPROACHES
from guardian.config import load_config
from guardian.experiment import (
    RUN_KEYS,
    RecordingLLM,
    RecordingStopped,
    RunResult,
    compare_models,
    compare_report,
    evaluate,
    experiment_config,
    experiment_report,
    format_results,
    freeze_rumors,
    mode_notice,
    record_all,
    run_experiment,
    scenario_paths,
)
from guardian.llm import LLMClient, LLMError, LLMRecordedFailureError, LLMUnusableReplyError
from guardian.scenario import load_scenario
from guardian.simulation import Simulation

GROQ_ENV = {"GROQ_API_KEY": "gsk_" + "x" * 24}


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def scenario(config):
    return load_scenario(config.experiment.scenario)


@pytest.fixture
def sandbox(config, tmp_path):
    """錄製和快照都放到暫存資料夾，不碰 repo 裡的檔案。"""
    settings = dataclasses.replace(config.experiment, snapshot_dir=tmp_path / "snapshot", pace_seconds=0)
    return dataclasses.replace(config, replay_cache_dir=tmp_path / "cache", experiment=settings,
                               data_165=dataclasses.replace(config.data_165, cache_dir=tmp_path / "data"))


def replay_factory(settings):
    return lambda **overrides: LLMClient.from_config(experiment_config(settings), mode="replay", env={}, **overrides)


# ---------------------------------------------------------------- 指標

def scored_run(config, scores, llm, *messages, mode="gentle"):
    engine = make_engine(config, llm, fast_filter=fixed_filter(scores))
    engine.speaking_mode = mode
    return evaluate(engine, scenario_of(*messages), "C")


def test_metrics_count_each_label_on_its_own(config):
    result = scored_run(
        config, {"s1": 80, "s2": 80, "inj": 80, "fp": 80}, Scripted(router=route_all("verify"),
                                                                     verifier=verify_json("high")),
        message("s1", 0, "scam"), message("s2", 60, "scam"), message("miss", 120, "scam"),
        message("inj", 180, "injection"), message("inj2", 240, "injection"),
        message("fp", 300), message("ok", 360),
    )
    assert (result.scam_total, result.scam_flagged, result.missed_scams) == (3, 2, ["miss"])
    assert (result.injection_total, result.injection_flagged, result.missed_injections) == (2, 1, ["inj2"])
    assert (result.normal_total, result.normal_flagged, result.false_alarms) == (2, 1, ["fp"])
    assert result.recall == pytest.approx(2 / 3)
    assert result.precision == pytest.approx(3 / 4)
    assert result.accuracy == pytest.approx(4 / 7)
    assert result.injection_success_rate == 0.5
    assert result.interruptions == 4
    assert result.interruptions_per_100 == pytest.approx(4 / 7 * 100)


def test_answers_to_mentions_are_not_interruptions_or_false_alarms(config):
    result = scored_run(config, {}, Scripted(), message("q", 0, text="@顧家貓 你好", mention=True))
    assert result.interruptions == 0 and result.normal_flagged == 0  # 中性回覆不算警示


def test_failed_verification_is_not_an_alert(config):
    result = scored_run(config, {"s": 80}, Scripted(verifier=RuntimeError("boom")),
                        message("s", 0, "scam", mention=True))
    assert result.scam_flagged == 0 and result.errors == 1


def test_medium_report_counts_as_an_alert_without_interrupting(config):
    result = scored_run(config, {"s": 80}, Scripted(router=route_all("verify"), verifier=verify_json("medium")),
                        message("s", 0, "scam"))
    assert result.scam_flagged == 1 and result.interruptions == 0


def test_latency_counts_messages_until_the_alert(config):
    result = scored_run(
        config, {"a": 80, "b": 80}, Scripted(router=route_all("verify"), verifier=verify_json("high")),
        message("a", 0, "scam"), message("b", 5, "scam"), message("n1", 10), message("n2", 60),
    )
    assert result.latencies == [0, 2]  # b 在 Router 忙的時候進來，等到 n2 才處理
    assert result.mean_latency == 1


def test_alert_waiting_for_the_end_of_the_scenario_is_still_counted(config):
    result = scored_run(
        config, {"a": 80, "b": 80}, Scripted(router=route_all("verify"), verifier=verify_json("high")),
        message("a", 0, "scam"), message("b", 5, "scam"),
    )
    assert result.scam_flagged == 2 and result.latencies == [0, 0]


def test_empty_categories_show_a_dash_instead_of_crashing():
    empty = RunResult("x", "x")
    assert empty.recall is None and empty.precision is None and empty.accuracy is None and empty.mean_latency is None
    row = format_results([empty]).splitlines()[-1]
    assert "—" in row and "0.0" in row


# ---------------------------------------------------------------- 三種做法和消融實驗

def test_offline_experiment_runs_every_approach_with_the_expected_shape(config, scenario):
    results = {result.key: result for result in run_experiment(config, scenario)}
    assert tuple(results) == RUN_KEYS
    assert all(result.messages == 198 and result.errors == 0 for result in results.values())
    assert results["A"].llm_calls == 0
    assert results["B"].llm_calls == 198  # 每則文字訊息一次
    assert results["C"].llm_calls < results["B"].llm_calls
    assert results["C-no-filter"].llm_calls > results["C"].llm_calls
    assert results["C-no-early"].llm_calls < results["C"].llm_calls
    assert results["C-no-batch"].llm_calls > results["C"].llm_calls  # 批次處理省下分流呼叫
    assert results["C-no-batch"].mean_latency < results["C"].mean_latency  # 代價是要等
    assert results["C"].injection_success_rate == 0  # 程式防線
    assert results["A"].missed_scams  # 規則抓不到的詐騙確實存在


def test_experiment_turns_the_watchdog_caps_off_and_speaks(config):
    settings = experiment_config(config)
    assert settings.speaking_mode == "gentle"
    assert settings.watchdog.max_reminders_per_100 > 1000 and settings.watchdog.max_error_rate == 1.0
    assert config.speaking_mode == "silent"  # 原本的設定不受影響


def test_watchdog_does_not_interfere_with_the_experiment(config, scenario):
    # 用預設的 Watchdog 上限跑，主動提醒會被切回 silent；放寬後才量得到真正的打擾次數
    capped = Simulation(config, scenario, "C")
    capped.engine.speaking_mode = "gentle"
    while capped.step():
        pass
    assert capped.engine.speaking_mode == "silent"
    assert any(notice.kind == "too_many_reminders" for notice in capped.engine.notices)
    relaxed = run_experiment(config, scenario, keys=("C",))[0]
    assert relaxed.interruptions > sum(1 for reply in capped.engine.replies)


def test_report_explains_itself_and_warns_about_the_fake_model(config):
    config = dataclasses.replace(config, llm_mode="offline")
    report = experiment_report(config)
    assert "offline 假模型" in report and "gentle" in report and "| C Super-agent |" in report
    assert "漏掉詐騙" in report and "失敗" not in report.split("漏掉和誤報")[0]


# ---------------------------------------------------------------- 模型比較

def test_compare_swaps_one_role_at_a_time(config, scenario):
    made = []

    def factory(**overrides):
        client = LLMClient("offline", overrides["models"], params=overrides["params"])
        made.append(client)
        return client

    rows = compare_models(config, scenario, factory)
    candidates = config.experiment.compare
    assert [(row.role, row.model) for row in rows] == [
        (role, item.model) for role, items in candidates.items() for item in items
    ]
    router_rows = len(candidates["router"])
    assert all(client.models["verifier"] == config.models["verifier"] for client in made[:router_rows])
    assert all(client.models["router"] == config.models["router"] for client in made[router_rows:])
    assert {row.result.messages for row in rows} == {30}


def test_candidate_params_can_drop_a_parameter(config):
    candidate = next(item for item in config.experiment.compare["verifier"] if item.params)
    params = experiment._candidate_params(config, "verifier", candidate)
    assert "reasoning_effort" not in params["verifier"]
    assert "reasoning_effort" in params["router"]


def test_compare_report_lists_every_candidate(config):
    report = compare_report(dataclasses.replace(config, llm_mode="offline"))
    for items in config.experiment.compare.values():
        for item in items:
            assert item.model in report


# ---------------------------------------------------------------- 回放（第 13 節第 12 項）

def test_replay_without_a_cache_warns_instead_of_crashing(sandbox):
    settings = dataclasses.replace(sandbox, llm_mode="replay")
    assert "還沒錄製" in mode_notice(settings)
    report = experiment_report(settings)
    assert "還沒錄完" in report and "不能當結果" in report


def test_mode_notice_says_where_results_come_from(sandbox):
    assert "假模型" in mode_notice(dataclasses.replace(sandbox, llm_mode="offline"))
    assert "花 token" in mode_notice(dataclasses.replace(sandbox, llm_mode="live"))


@pytest.fixture
def recorded(sandbox):
    """用假的 aisuite 錄一次全部的呼叫。"""
    aisuite = RecordingAisuite()
    totals = record_all(sandbox, log=lambda line: None, live_client=aisuite, env=GROQ_ENV, sleep=lambda seconds: None)
    return sandbox, aisuite, totals


def test_recording_then_replay_needs_no_key_and_gives_the_same_results(recorded):
    settings, aisuite, totals = recorded
    assert totals["recorded"] == aisuite.calls > 0
    assert totals["tokens"] == aisuite.calls * 15  # 假的 aisuite 每次回報 15 token
    replay = dataclasses.replace(settings, llm_mode="replay")
    assert "不需要金鑰" in mode_notice(replay)
    scenario = load_scenario(settings.experiment.scenario)
    live_like = run_experiment(dataclasses.replace(settings, llm_mode="offline"), scenario)
    replayed = run_experiment(settings, scenario, replay_factory(settings))
    assert all(result.errors == 0 for result in replayed)
    for offline_result, replay_result in zip(live_like, replayed):
        assert offline_result.scam_flagged == replay_result.scam_flagged
        assert offline_result.false_alarms == replay_result.false_alarms
        assert offline_result.llm_calls == replay_result.llm_calls
    assert all(row.result.errors == 0 for row in compare_models(settings, scenario, replay_factory(settings)))
    # 還有 token：回放時從快取讀回來
    assert next(result for result in replayed if result.key == "C").tokens > 0


@pytest.mark.parametrize("mode", ["silent", "gentle", "on_mention"])
def test_recording_covers_every_simulator_combination(recorded, mode):
    settings, _, _ = recorded
    for path in scenario_paths(settings):
        for approach in SIMULATOR_APPROACHES:
            client = LLMClient.from_config(settings, mode="replay", env={})
            simulation = Simulation.from_path(settings, path, approach, mode, llm_client=client)
            while simulation.step():
                pass
            assert client.stats.total_errors == 0, (path.name, approach, mode)


def test_recording_twice_does_not_spend_tokens_again(recorded):
    settings, aisuite, _ = recorded
    calls = aisuite.calls
    again = RecordingAisuite()
    totals = record_all(settings, log=lambda line: None, live_client=again, env=GROQ_ENV, sleep=lambda seconds: None)
    assert again.calls == 0 and totals["recorded"] == 0 and totals["reused"] >= calls
    assert totals["tokens"] == 0  # 沿用的不算這次花的


def test_a_failure_stops_the_recording_and_the_next_run_picks_it_up(sandbox):
    first = RecordingAisuite(fail_after=5)
    options = dict(log=lambda line: None, env=GROQ_ENV, sleep=lambda seconds: None)
    with pytest.raises(RecordingStopped):
        record_all(sandbox, live_client=first, **options)
    assert first.calls == 5

    second = RecordingAisuite()
    record_all(sandbox, live_client=second, **options)
    fresh = RecordingAisuite()
    record_all(dataclasses.replace(sandbox, replay_cache_dir=sandbox.replay_cache_dir.parent / "other"),
               live_client=fresh, **options)
    assert second.calls == fresh.calls - 5  # 前面錄好的 5 次沒有再花一次


def test_blank_replies_are_asked_again_and_counted(sandbox):
    waits = []
    client = RecordingLLM("live", sandbox.models, cache_dir=sandbox.replay_cache_dir,
                          live_client=RecordingAisuite(blanks=2), env=GROQ_ENV, pace_seconds=5, sleep=waits.append)
    prompt = '{"task":"summarize"}'
    assert client.call("reporter", "系統", prompt)
    assert client.unusable_replies == 2 and client.recorded == 1 and client.gave_up == 0
    assert waits == [5, 5, 5]  # 重問也要等，不會超過每分鐘上限
    assert len(list(sandbox.replay_cache_dir.rglob("*.json"))) == 1  # 只存有內容的那一次


def test_a_prompt_that_stays_unusable_is_recorded_as_a_failure_and_replayed_the_same(sandbox):
    aisuite = RecordingAisuite(blanks=99)
    client = RecordingLLM("live", sandbox.models, cache_dir=sandbox.replay_cache_dir, live_client=aisuite, env=GROQ_ENV)
    prompt = '{"task":"summarize"}'
    with pytest.raises(LLMUnusableReplyError):
        client.call("reporter", "系統", prompt)  # 不是 RecordingStopped：這是模型的結果，錄製要繼續
    assert aisuite.calls == 3 and client.gave_up == 1

    with pytest.raises(LLMRecordedFailureError):
        client.call("reporter", "系統", prompt)  # 再錄一次不會再花 token
    assert aisuite.calls == 3

    replay = LLMClient("replay", sandbox.models, cache_dir=sandbox.replay_cache_dir, env={})
    with pytest.raises(LLMRecordedFailureError):
        replay.call("reporter", "系統", prompt)
    assert replay.stats.total_errors == 1 and replay.stats.replay_misses == 0


def test_recorded_failures_are_part_of_the_result_not_a_missing_cache():
    assert "還沒錄完" in experiment._error_notice([RunResult("x", "x", errors=3, replay_misses=3)])
    notice = experiment._error_notice([RunResult("x", "x", errors=3)])
    assert "給不出能用的回覆" in notice and "還沒錄完" not in notice
    assert experiment._error_notice([RunResult("x", "x")]) == ""


def test_blank_replies_are_reported_after_recording(sandbox):
    lines = []
    totals = record_all(sandbox, log=lines.append, live_client=RecordingAisuite(blanks=1), env=GROQ_ENV,
                        sleep=lambda seconds: None)
    assert totals["unusable_replies"] == 1 and totals["gave_up"] == 0 and totals["recorded"] > 0
    assert any("回覆不能用 1 次" in line for line in lines)


def test_outside_of_recording_a_blank_reply_is_an_ordinary_failure(sandbox):
    # 正式執行不重問：記錄錯誤、走失敗的路徑，交給 Watchdog（CLAUDE.md）
    client = LLMClient("live", sandbox.models, cache_dir=sandbox.replay_cache_dir,
                       live_client=RecordingAisuite(blanks=1), env=GROQ_ENV)
    with pytest.raises(LLMError, match="空的"):
        client.call("reporter", "系統", '{"task":"summarize"}')
    assert client.stats.total_errors == 1


def test_recording_without_a_key_stops_right_away_with_a_clear_message(sandbox):
    with pytest.raises(RecordingStopped, match="GROQ_API_KEY"):
        record_all(sandbox, log=lambda line: None, env={})


def test_recording_waits_between_real_calls_only(sandbox):
    waits = []
    client = RecordingLLM("live", sandbox.models, cache_dir=sandbox.replay_cache_dir, live_client=RecordingAisuite(),
                          env=GROQ_ENV, pace_seconds=5, sleep=waits.append)
    prompt = '{"task":"summarize"}'
    assert client.call("reporter", "系統", prompt) == client.call("reporter", "系統", prompt)
    assert waits == [5] and (client.recorded, client.reused) == (1, 1)


def test_a_recording_failure_is_not_treated_as_an_ordinary_llm_error(sandbox):
    # 引擎遇到一般失敗會合併重試，會錄進本來不會出現的 prompt，所以錄製要整個停下來
    client = RecordingLLM("live", sandbox.models, cache_dir=sandbox.replay_cache_dir,
                          live_client=RecordingAisuite(fail_after=0), env=GROQ_ENV)
    engine = make_engine(sandbox, fast_filter=fixed_filter({"a": 80}))
    engine.llm = client
    engine.router.client = client
    with pytest.raises(RecordingStopped):
        engine.process(message("a").to_raw_event("g"))


# ---------------------------------------------------------------- 闢謠資料凍結（決策 D35）

def test_freeze_copies_once_and_later_updates_do_not_change_the_snapshot(sandbox):
    assert "還沒下載" in freeze_rumors(sandbox)
    source = sandbox.data_165.cache_dir / "rumors.csv"
    source.parent.mkdir(parents=True)
    source.write_text("title,content\n舊標題,舊內容\n", encoding="utf-8")
    assert "已凍結" in freeze_rumors(sandbox)
    source.write_text("title,content\n新標題,新內容\n", encoding="utf-8")
    assert "沿用" in freeze_rumors(sandbox)
    target = sandbox.experiment.snapshot_dir / "rumors.csv"
    assert "舊標題" in target.read_text(encoding="utf-8")


def test_frozen_rumors_are_what_the_engines_use(sandbox):
    target = sandbox.experiment.snapshot_dir / "rumors.csv"
    target.parent.mkdir(parents=True)
    target.write_text("title,content\n解除分期闢謠,解除分期的說明\n", encoding="utf-8")
    simulation = Simulation(sandbox, scenario_of(message("a")))
    assert len(simulation.engine.verifier.rumors) == 1


# ---------------------------------------------------------------- 其他

def test_scenario_paths_put_the_demo_first(config):
    paths = scenario_paths(config)
    assert paths[0].name == "demo_family.yaml" and any(path.name == "experiment.yaml" for path in paths)


def test_cli_offline_prints_both_tables_and_saves_them(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, "load_env_file", lambda *args, **kwargs: False)
    output = tmp_path / "result.md"
    assert experiment.main(["--mode", "offline", "--output", str(output)]) == 0
    out = capsys.readouterr().out
    assert "### 三種做法" in out and "### 模型比較" in out
    assert "| C Super-agent |" in output.read_text(encoding="utf-8")


def test_cli_can_skip_the_comparison(capsys, monkeypatch):
    monkeypatch.setattr(experiment, "load_env_file", lambda *args, **kwargs: False)
    assert experiment.main(["--mode", "offline", "--no-compare"]) == 0
    assert "### 模型比較" not in capsys.readouterr().out


def test_cli_recording_failure_explains_what_to_do(capsys, monkeypatch):
    def stopped(config, **kwargs):
        raise RecordingStopped("429 rate limit，金鑰 gsk_abcdefgh12345678")

    monkeypatch.setattr(experiment, "load_env_file", lambda *args, **kwargs: False)
    monkeypatch.setattr(experiment, "record_all", stopped)
    assert experiment.main(["--record"]) == 1
    err = capsys.readouterr().err
    assert "錄製中斷" in err and "接著錄" in err
    assert "gsk_abcdefgh" not in err  # 金鑰不印出來
