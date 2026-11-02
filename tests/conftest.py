"""測試共用設定：全部測試都在 offline 模式下跑，不讀金鑰、不連網，也不讀這台電腦上下載的 165 名單。"""

from pathlib import Path

import pytest

from guardian import llm
from guardian.config import load_config
from guardian.data_165 import DomainList, RumorIndex

KEY_ENVS = ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
DOWNLOADED_165 = Path(load_config().data_165.cache_dir).resolve()


def _skip_downloaded(original):
    def from_cache(cls, cache_dir, *args, **kwargs):
        if Path(cache_dir).resolve() == DOWNLOADED_165:
            return cls()
        return original(cls, cache_dir, *args, **kwargs)
    return classmethod(from_cache)


@pytest.fixture(autouse=True)
def no_downloaded_165(monkeypatch):
    """下載的名單每週會變，結果不該跟著變；每建一個引擎都讀 11 萬筆也會拖慢測試（決策 D58）。"""
    for index in (DomainList, RumorIndex):
        monkeypatch.setattr(index, "from_cache", _skip_downloaded(index.from_cache.__func__))


@pytest.fixture(autouse=True)
def offline_mode(monkeypatch):
    monkeypatch.setenv("GUARDIAN_LLM_MODE", "offline")
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    llm.reset_default_client()
    yield
    llm.reset_default_client()
