"""測試共用設定：全部測試都在 offline 模式下跑，不讀金鑰、不連網。"""

import pytest

from guardian import llm

KEY_ENVS = ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def offline_mode(monkeypatch):
    monkeypatch.setenv("GUARDIAN_LLM_MODE", "offline")
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    llm.reset_default_client()
    yield
    llm.reset_default_client()
