"""設定檔讀取與檢查（SPEC 第 8 節）。"""

import pytest
import yaml

from guardian.config import DEFAULT_CONFIG_PATH, ConfigError, load_config


def write_config(tmp_path, mutate):
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_default_config_is_valid():
    config = load_config(env={})
    assert config.llm_mode == "replay"  # 預設 replay（SPEC 第 8 節）
    assert set(config.models) == {"router", "verifier", "reporter"}
    assert (config.thresholds.watch, config.thresholds.candidate) == (30, 60)
    assert config.speaking_mode == "silent"  # 預設不在群組發言
    assert config.max_reply_chars == 100
    assert config.thread_ttl_hours == 24
    assert config.router_latency_seconds == 20
    assert (config.data_165.rumors.dataset_id, config.data_165.rumors.kind) == ("38262", "rumors")
    assert config.record_retention_days == 30
    assert config.hash_salt_env == "GUARDIAN_HASH_SALT"
    assert config.replay_cache_write is True  # 模擬資料用；LINE 伺服器會強制關閉（決策 D2）
    assert [s.id for s in config.data_165.sources] == ["fake_investment", "suspended_sites"]
    assert config.data_165.sources[0].dataset_id == "160055"
    assert config.data_165.max_age_days == 7  # 每週更新
    assert config.data_165.cache_dir.parts[-2:] == ("data", "165")


def test_config_file_contains_no_secrets():
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    for marker in ("gsk_", "sk-", "AIza"):
        assert marker not in text


def test_env_overrides_mode():
    assert load_config(env={"GUARDIAN_LLM_MODE": "offline"}).llm_mode == "offline"


def test_blank_env_mode_is_ignored():
    assert load_config(env={"GUARDIAN_LLM_MODE": "  "}).llm_mode == "replay"


def test_invalid_env_mode_is_rejected():
    with pytest.raises(ConfigError, match="GUARDIAN_LLM_MODE"):
        load_config(env={"GUARDIAN_LLM_MODE": "online"})


@pytest.mark.parametrize("mutate", [
    lambda d: d["thresholds"].update(watch=60, candidate=30),
    lambda d: d["thresholds"].update(candidate=120),
    lambda d: d["models"].pop("verifier"),
    lambda d: d["models"].update(router="gpt-oss-120b"),  # 少了供應商
    lambda d: d.update(llm_mode="cloud"),
    lambda d: d["speaking"].update(mode="loud"),
    lambda d: d["speaking"].update(max_reply_chars=0),
    lambda d: d["model_params"].update(painter={"temperature": 1}),
    lambda d: d.pop("privacy"),
    lambda d: d.update(replay_cache_write="yes"),
    lambda d: d["data_165"].update(sources=[]),
    lambda d: d["data_165"]["sources"].append(dict(d["data_165"]["sources"][0])),  # id 重複
    lambda d: d["data_165"]["sources"][0].update(dataset_id="abc"),
    lambda d: d["data_165"]["sources"][0].update(dataset_id=None),  # 沒有 dataset_id 也沒有 download_url
    lambda d: d["data_165"]["sources"][0].update(download_url="ftp://files.example/a.csv"),
    lambda d: d["data_165"].update(max_age_days=0),
])
def test_invalid_config_is_rejected(tmp_path, mutate):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, mutate), env={})


def test_params_for_merges_role_and_drops_null(tmp_path):
    def mutate(data):
        data["model_params"]["verifier"] = {"reasoning_effort": None, "temperature": 0}

    config = load_config(write_config(tmp_path, mutate), env={})
    assert config.params_for("router") == {"temperature": 0.3, "reasoning_effort": "low",
                                           "max_completion_tokens": 8192}
    assert config.params_for("verifier") == {"temperature": 0, "max_completion_tokens": 8192}


def test_cache_dir_is_relative_to_config_file(tmp_path):
    config = load_config(write_config(tmp_path, lambda d: None), env={})
    assert config.replay_cache_dir == tmp_path.resolve() / "replay_cache"


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="找不到設定檔"):
        load_config(tmp_path / "nope.yaml", env={})
