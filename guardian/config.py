"""讀取與檢查 config.yaml（SPEC 第 8 節）。

設定檔不放金鑰。金鑰只放 .env，由 load_env_file() 載入成環境變數，程式不印出它們。
"""

from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from .data_165 import Source
from .llm import MODES, ROLES

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
MODE_ENV = "GUARDIAN_LLM_MODE"
SPEAKING_MODES = ("silent", "gentle", "on_mention")


class ConfigError(ValueError):
    """設定檔內容不正確。"""


@dataclass(frozen=True)
class Thresholds:
    watch: int
    candidate: int


@dataclass(frozen=True)
class Data165Settings:
    cache_dir: Path
    max_age_days: int
    sources: tuple[Source, ...]
    rumors: Source


@dataclass(frozen=True)
class Config:
    models: dict[str, str]
    llm_mode: str
    replay_cache_dir: Path
    replay_cache_write: bool
    model_params: dict[str, dict[str, Any]]
    thresholds: Thresholds
    data_165: Data165Settings
    speaking_mode: str
    max_reply_chars: int
    thread_ttl_hours: int
    router_latency_seconds: int
    record_retention_days: int
    hash_salt_env: str

    def params_for(self, role: str) -> dict[str, Any]:
        """default 參數加上角色自己的參數；值是 null 的參數會被拿掉。"""
        merged = {**self.model_params.get("default", {}), **self.model_params.get(role, {})}
        return {key: value for key, value in merged.items() if value is not None}


def _get(data: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ConfigError(f"{where} 缺少 {key}")
    return data[key]


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} 必須是對應表（key: value）")
    return value


def _int(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} 必須是整數")
    if value < minimum or (maximum is not None and value > maximum):
        upper = f"、不大於 {maximum}" if maximum is not None else ""
        raise ConfigError(f"{name} 必須不小於 {minimum}{upper}，收到：{value}")
    return value


def _choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ConfigError(f"{name} 必須是 {'、'.join(choices)} 其中之一，收到：{value!r}")
    return value


def _path(value: Any, name: str, config_path: Path) -> Path:
    """相對路徑以設定檔所在的資料夾為準。"""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} 必須是資料夾路徑")
    path = Path(value)
    return path if path.is_absolute() else config_path.resolve().parent / path


_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


def _sources(value: Any) -> tuple[Source, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("data_165.sources 必須是非空清單")
    sources: list[Source] = []
    for index, item in enumerate(value, start=1):
        where = f"data_165.sources 第 {index} 項"
        item = _mapping(item, where)
        source_id = item.get("id")
        if not isinstance(source_id, str) or not _SOURCE_ID.match(source_id):
            raise ConfigError(f"{where}：id 必須是小寫英文、數字或底線")
        if any(source.id == source_id for source in sources):
            raise ConfigError(f"data_165.sources 的 id 重複：{source_id}")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where}：name 必須是非空字串")
        dataset_id = item.get("dataset_id")
        if dataset_id is not None:
            dataset_id = str(dataset_id).strip()
            if not dataset_id.isdigit():
                raise ConfigError(f"{where}：dataset_id 必須是 data.gov.tw 的數字編號")
        download_url = item.get("download_url")
        if download_url is not None and (
            not isinstance(download_url, str) or not download_url.startswith(("http://", "https://"))
        ):
            raise ConfigError(f"{where}：download_url 必須是 http 或 https 網址")
        if not dataset_id and not download_url:
            raise ConfigError(f"{where}：dataset_id 和 download_url 至少要有一個")
        expect_title = item.get("expect_title")
        if expect_title is not None and (not isinstance(expect_title, str) or not expect_title.strip()):
            raise ConfigError(f"{where}：expect_title 必須是非空字串")
        sources.append(Source(id=source_id, name=name.strip(), dataset_id=dataset_id,
                              download_url=download_url, expect_title=expect_title))
    return tuple(sources)


def load_config(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """讀取設定檔並檢查。環境變數 GUARDIAN_LLM_MODE 有值時覆蓋 llm_mode。"""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    env = os.environ if env is None else env
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"找不到設定檔：{config_path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"設定檔不是合法的 YAML：{config_path.name}（{exc}）") from None
    where = config_path.name
    raw = _mapping(raw, where)

    models = _mapping(_get(raw, "models", where), f"{where} 的 models")
    for role in ROLES:
        model = _get(models, role, f"{where} 的 models")
        provider, _, name = str(model).partition(":") if isinstance(model, str) else ("", "", "")
        if not provider or not name:
            raise ConfigError(f"models.{role} 必須是「供應商:模型」格式，例如 groq:openai/gpt-oss-120b")

    llm_mode = _choice(_get(raw, "llm_mode", where), "llm_mode", MODES)
    override = (env.get(MODE_ENV) or "").strip()
    if override:
        llm_mode = _choice(override, f"環境變數 {MODE_ENV}", MODES)

    cache_path = _path(_get(raw, "replay_cache_dir", where), "replay_cache_dir", config_path)
    cache_write = _get(raw, "replay_cache_write", where)
    if not isinstance(cache_write, bool):
        raise ConfigError("replay_cache_write 必須是 true 或 false")

    data_raw = _mapping(_get(raw, "data_165", where), "data_165")
    data_165 = Data165Settings(
        cache_dir=_path(_get(data_raw, "cache_dir", "data_165"), "data_165.cache_dir", config_path),
        max_age_days=_int(_get(data_raw, "max_age_days", "data_165"), "data_165.max_age_days", 1),
        sources=_sources(_get(data_raw, "sources", "data_165")),
        rumors=replace(_sources([_get(data_raw, "rumors", "data_165")])[0], kind="rumors"),
    )

    model_params = _mapping(raw.get("model_params") or {}, "model_params")
    for section, values in model_params.items():
        if section != "default" and section not in ROLES:
            raise ConfigError(f"model_params 只能有 default 和 {'、'.join(ROLES)}，收到：{section!r}")
        _mapping(values, f"model_params.{section}")

    thresholds_raw = _mapping(_get(raw, "thresholds", where), "thresholds")
    watch = _int(_get(thresholds_raw, "watch", "thresholds"), "thresholds.watch", 0, 100)
    candidate = _int(_get(thresholds_raw, "candidate", "thresholds"), "thresholds.candidate", 0, 100)
    if watch >= candidate:
        raise ConfigError("thresholds.watch 必須小於 thresholds.candidate")

    speaking = _mapping(_get(raw, "speaking", where), "speaking")
    privacy = _mapping(_get(raw, "privacy", where), "privacy")
    hash_salt_env = _get(privacy, "hash_salt_env", "privacy")
    if not isinstance(hash_salt_env, str) or not hash_salt_env.strip():
        raise ConfigError("privacy.hash_salt_env 必須是環境變數名稱")

    return Config(
        models={role: models[role] for role in ROLES},
        llm_mode=llm_mode,
        replay_cache_dir=cache_path,
        replay_cache_write=cache_write,
        model_params={section: dict(values) for section, values in model_params.items()},
        thresholds=Thresholds(watch=watch, candidate=candidate),
        data_165=data_165,
        speaking_mode=_choice(_get(speaking, "mode", "speaking"), "speaking.mode", SPEAKING_MODES),
        max_reply_chars=_int(_get(speaking, "max_reply_chars", "speaking"), "speaking.max_reply_chars", 1),
        thread_ttl_hours=_int(_get(raw, "thread_ttl_hours", where), "thread_ttl_hours", 1),
        router_latency_seconds=_int(_get(raw, "router_latency_seconds", where), "router_latency_seconds", 0),
        record_retention_days=_int(
            _get(privacy, "record_retention_days", "privacy"), "privacy.record_retention_days", 1
        ),
        hash_salt_env=hash_salt_env,
    )


def load_env_file(path: str | Path | None = None) -> bool:
    """載入 .env 成環境變數（不覆蓋已經設定的值）。回傳是否有載入。"""
    env_path = Path(path) if path is not None else PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        warnings.warn("找到 .env，但沒有安裝 python-dotenv；請先執行 pip install -r requirements.txt", stacklevel=2)
        return False
    return bool(load_dotenv(env_path, override=False))
