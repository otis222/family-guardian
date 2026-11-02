"""真實錄好的回放（SPEC 第 13 節第 12 項）：沒有金鑰也能跑完，結果和錄製時一致。

這些數字是 2026-11-02 用 gpt-oss-120b 錄的。改了 prompt、模型或實驗劇本，這裡就會失敗：
要重新錄製（python -m guardian.experiment --record），再更新這裡的數字。
"""

import dataclasses

import pytest

from guardian.approaches import SIMULATOR_APPROACHES
from guardian.config import SPEAKING_MODES, load_config
from guardian.experiment import compare_models, run_experiment, scenario_paths
from guardian.llm import LLMClient
from guardian.scenario import load_scenario
from guardian.simulation import Simulation

# (抓到的詐騙, 誤報, 主動提醒, 模型呼叫, token, 注入成功)
RECORDED = {
    "A": (16, 19, 18, 0, 0, 0),
    "B": (19, 8, 27, 198, 94786, 0),
    "C": (15, 4, 24, 110, 61734, 0),
    "C-no-filter": (20, 6, 29, 313, 159408, 0),
    "C-no-early": (12, 1, 15, 71, 38867, 3),
    "C-no-batch": (15, 4, 24, 113, 62570, 0),
}


@pytest.fixture
def config():
    return dataclasses.replace(load_config(), llm_mode="replay")


def test_experiment_replays_without_a_key_and_matches_the_recording(config, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    results = run_experiment(config, load_scenario(config.experiment.scenario))
    for result in results:
        assert result.replay_misses == 0 and result.errors == 0, result.key
        assert (result.scam_flagged, result.normal_flagged, result.interruptions, result.llm_calls, result.tokens,
                result.injection_total - result.injection_flagged) == RECORDED[result.key], result.key


def test_model_comparison_replays_completely(config):
    rows = compare_models(config, load_scenario(config.experiment.scenario))
    assert len(rows) == sum(len(items) for items in config.experiment.compare.values())
    assert all(row.result.replay_misses == 0 for row in rows)


@pytest.mark.parametrize("approach", SIMULATOR_APPROACHES)
@pytest.mark.parametrize("mode", SPEAKING_MODES)
def test_simulator_has_every_response_it_needs(config, approach, mode):
    for path in scenario_paths(config):
        client = LLMClient.from_config(config, env={})
        simulation = Simulation.from_path(config, path, approach, mode, llm_client=client)
        while simulation.step():
            pass
        assert client.stats.replay_misses == 0 and client.stats.total_errors == 0, path.name
