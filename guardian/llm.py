"""所有模型呼叫的唯一入口：call_llm(role, system_prompt, user_prompt)（SPEC 第 8 節）。

三種模式：
- offline：假模型，給測試用。不讀金鑰、不連網。
- replay：讀 replay_cache/ 裡錄好的回應，不需要金鑰。找不到就丟出 ReplayMissError，不會偷偷改用 live。
- live：透過老師的 aisuite 真的呼叫模型，成功後把回應寫進快取。

快取檔只存 prompt 的雜湊、模型名稱、回應和 token 用量，不存 prompt 原文。
任何失敗都不自動重試：計入錯誤統計、丟出 LLMError，交給上層（Watchdog）處理。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .models import SchemaError

if TYPE_CHECKING:
    from .config import Config

MODES = ("offline", "replay", "live")
ROLES = ("router", "verifier", "reporter")

# live 模式呼叫前先確認金鑰有設定（只檢查有沒有值，不讀出、不印出）
PROVIDER_KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
# 錯誤訊息裡出現這些環境變數的值時一律遮掉
SECRET_ENV_NAMES = (
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LINE_CHANNEL_SECRET",
    "LINE_CHANNEL_ACCESS_TOKEN",
    "GUARDIAN_HASH_SALT",
)

Responder = Callable[[str, str, str], str]  # (role, system_prompt, user_prompt) -> 回覆文字


class LLMError(RuntimeError):
    """模型呼叫失敗。訊息已經遮掉金鑰，可以直接顯示。"""


class ReplayMissError(LLMError):
    """replay 模式在快取裡找不到這次呼叫。"""


class LLMCallError(LLMError):
    """live 呼叫失敗、回覆空白，或缺少金鑰、套件。"""


# ---------------------------------------------------------------- 工具函式

_SECRET_PATTERNS = (
    re.compile(r"gsk_[A-Za-z0-9]{8,}"),  # Groq
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),  # OpenAI、Anthropic
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),  # Google
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}")


def safe_error(err: BaseException | str, limit: int = 300, env: Mapping[str, str] | None = None) -> str:
    """把錯誤轉成可以顯示的一行文字：遮掉金鑰、壓縮空白、限制長度。"""
    env = os.environ if env is None else env
    text = f"{type(err).__name__}: {err}" if isinstance(err, BaseException) else str(err)
    for name in SECRET_ENV_NAMES:
        value = env.get(name) or ""
        if len(value) >= 6:
            text = text.replace(value, "***")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***", text)
    text = _BEARER.sub(r"\1***", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def cache_key(role: str, model: str, system_prompt: str, user_prompt: str) -> str:
    """回放快取的 key：角色、模型、兩段 prompt 任何一個改變，key 就不同。"""
    payload = json.dumps([role, model, system_prompt, user_prompt], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """從模型回覆取出 JSON。容許 ```json 區塊，或 JSON 前後多了說明文字。"""
    if not isinstance(text, str) or not text.strip():
        raise SchemaError("模型回覆是空的，沒有 JSON")
    decoder = json.JSONDecoder()
    for candidate in [match.group(1) for match in _FENCE.finditer(text)] + [text]:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for index, char in enumerate(candidate):
            if char in "{[":
                try:
                    value, _ = decoder.raw_decode(candidate, index)
                    return value
                except json.JSONDecodeError:
                    continue
    raise SchemaError("模型回覆裡找不到合法的 JSON")


_EVENT_ID = re.compile(r'"event_id"\s*:\s*"([^"]+)"')


def offline_responder(role: str, system_prompt: str, user_prompt: str) -> str:
    """offline 模式的假模型：依角色回傳固定、符合 schema 的內容。

    - router：替 user prompt 裡出現的每個 "event_id" 回一個 ignore 決策
    - verifier：固定回 low
    - reporter：固定一段文字
    測試需要其他行為時，用 LLMClient(responder=...) 注入自己的假函式。
    """
    if role == "router":
        event_ids = list(dict.fromkeys(_EVENT_ID.findall(user_prompt)))
        decisions = [
            {"event_id": event_id, "action": "ignore", "reason": "offline 假模型的固定回應"}
            for event_id in event_ids
        ]
        return json.dumps({"decisions": decisions}, ensure_ascii=False)
    if role == "verifier":
        return json.dumps(
            {"risk_level": "low", "reasons": ["offline 假模型的固定回應"], "evidence": [], "advice": []},
            ensure_ascii=False,
        )
    if role == "reporter":
        return "（offline 假模型）固定的守護人報告。"
    raise ValueError(f"未知的角色：{role!r}")


def _usage_dict(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    else:
        prompt, completion = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
    if prompt is None and completion is None:
        return None
    return {"prompt_tokens": int(prompt or 0), "completion_tokens": int(completion or 0)}


# ---------------------------------------------------------------- 統計與 client


@dataclass
class LLMStats:
    """呼叫統計（SPEC 11-1 的「LLM 呼叫數與 token」，也給 Watchdog 看預算）。"""

    calls: Counter = field(default_factory=Counter)  # 依角色計算的嘗試次數
    errors: Counter = field(default_factory=Counter)
    cache_hits: int = 0
    cache_writes: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    @property
    def total_errors(self) -> int:
        return sum(self.errors.values())

    def add_usage(self, usage: Mapping[str, int] | None) -> None:
        if usage:
            self.prompt_tokens += int(usage.get("prompt_tokens", 0))
            self.completion_tokens += int(usage.get("completion_tokens", 0))

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": {role: self.calls.get(role, 0) for role in ROLES},
            "total_calls": self.total_calls,
            "errors": {role: self.errors.get(role, 0) for role in ROLES},
            "total_errors": self.total_errors,
            "cache_hits": self.cache_hits,
            "cache_writes": self.cache_writes,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class LLMClient:
    """依模式處理模型呼叫，並累計統計。一般程式碼不直接用它，而是呼叫 call_llm()。"""

    def __init__(
        self,
        mode: str,
        models: Mapping[str, str],
        *,
        cache_dir: str | Path | None = None,
        params: Mapping[str, Mapping[str, Any]] | None = None,
        responder: Responder | None = None,
        live_client: Any = None,
        env: Mapping[str, str] | None = None,
        write_cache: bool = True,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"llm 模式必須是 {'、'.join(MODES)} 其中之一，收到：{mode!r}")
        if mode in ("replay", "live") and cache_dir is None:
            raise ValueError("replay 和 live 模式需要指定回放快取資料夾")
        self.mode = mode
        self.models = dict(models)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.params = {role: dict(values) for role, values in (params or {}).items()}
        self.responder = responder or offline_responder
        self._live_client = live_client
        self._env = env  # None 代表每次都讀 os.environ
        self.write_cache = write_cache  # live 模式要不要寫回放快取；真實訊息一律 False（決策 D2）
        self.stats = LLMStats()

    @classmethod
    def from_config(cls, config: Config, **overrides: Any) -> LLMClient:
        options: dict[str, Any] = {
            "cache_dir": config.replay_cache_dir,
            "params": {role: config.params_for(role) for role in ROLES},
            "write_cache": config.replay_cache_write,
        }
        options.update(overrides)
        mode = options.pop("mode", config.llm_mode)
        models = options.pop("models", config.models)
        return cls(mode, models, **options)

    @property
    def env(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def call(self, role: str, system_prompt: str, user_prompt: str) -> str:
        if role not in ROLES:
            raise ValueError(f"未知的角色：{role!r}（可用：{'、'.join(ROLES)}）")
        if not isinstance(system_prompt, str) or not isinstance(user_prompt, str):
            raise TypeError("system_prompt 和 user_prompt 必須是字串")
        model = self.models.get(role)
        if not model:
            raise ValueError(f"config.yaml 的 models 沒有設定 {role}")

        self.stats.calls[role] += 1
        try:
            if self.mode == "offline":
                text = self.responder(role, system_prompt, user_prompt)
            elif self.mode == "replay":
                text = self._replay(role, model, system_prompt, user_prompt)
            else:
                text = self._live(role, model, system_prompt, user_prompt)
            if not isinstance(text, str) or not text.strip():
                raise LLMCallError(f"{role} 的模型回覆是空的（未自動重試）")
        except LLMError:
            self.stats.errors[role] += 1
            raise
        except Exception as exc:  # 供應商的各種錯誤：遮掉金鑰後轉成 LLMCallError
            self.stats.errors[role] += 1
            raise LLMCallError(f"{role} 呼叫失敗（未自動重試）：{safe_error(exc, env=self.env)}") from None
        return text

    # -------------------------------------------------------------- replay

    def _cache_path(self, role: str, key: str) -> Path:
        assert self.cache_dir is not None
        return self.cache_dir / role / f"{key}.json"

    def _replay(self, role: str, model: str, system_prompt: str, user_prompt: str) -> str:
        key = cache_key(role, model, system_prompt, user_prompt)
        path = self._cache_path(role, key)
        if not path.is_file():
            raise ReplayMissError(
                f"回放快取裡找不到這次 {role} 呼叫（key {key[:12]}…）。"
                "請先用 live 模式錄製，或確認 prompt 和模型設定都沒有改動。"
            )
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReplayMissError(f"回放快取檔損壞：{path.name}（{safe_error(exc)}）") from None
        if not isinstance(entry, dict) or entry.get("key") != key or not isinstance(entry.get("response"), str):
            raise ReplayMissError(f"回放快取檔內容不符：{path.name}")
        self.stats.cache_hits += 1
        self.stats.add_usage(entry.get("usage"))
        return entry["response"]

    # -------------------------------------------------------------- live

    def _get_live_client(self) -> Any:
        if self._live_client is None:
            try:
                import aisuite as ai
            except ImportError:
                raise LLMCallError("live 模式需要 aisuite，請先執行 pip install -r requirements.txt") from None
            self._live_client = ai.Client()
        return self._live_client

    def _live(self, role: str, model: str, system_prompt: str, user_prompt: str) -> str:
        key_env = PROVIDER_KEY_ENV.get(model.split(":", 1)[0])
        if key_env and not self.env.get(key_env):
            raise LLMCallError(f"live 模式需要環境變數 {key_env}（放在 .env，不要寫進程式或 notebook）")
        response = self._get_live_client().chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **self.params.get(role, {}),
        )
        text = response.choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise LLMCallError(f"{role} 的模型回覆是空的（未自動重試）")
        usage = _usage_dict(getattr(response, "usage", None))
        self.stats.add_usage(usage)
        if self.write_cache:
            self._write_cache(role, model, cache_key(role, model, system_prompt, user_prompt), text, usage)
        return text

    def _write_cache(self, role: str, model: str, key: str, text: str, usage: dict[str, int] | None) -> None:
        path = self._cache_path(role, key)
        entry = {
            "key": key,
            "role": role,
            "model": model,
            "response": text,
            "usage": usage,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            raise LLMCallError(f"已取得 {role} 的回應，但寫入回放快取失敗：{safe_error(exc)}") from None
        self.stats.cache_writes += 1


# ---------------------------------------------------------------- 唯一入口

_default_client: LLMClient | None = None


def set_default_client(client: LLMClient | None) -> None:
    global _default_client
    _default_client = client


def reset_default_client() -> None:
    set_default_client(None)


def get_default_client() -> LLMClient:
    """第一次使用時依 config.yaml（和 GUARDIAN_LLM_MODE）建立預設 client。"""
    global _default_client
    if _default_client is None:
        from .config import load_config

        _default_client = LLMClient.from_config(load_config())
    return _default_client


def call_llm(role: str, system_prompt: str, user_prompt: str, *, client: LLMClient | None = None) -> str:
    """所有模型呼叫都經過這裡。client 省略時用預設 client。"""
    return (client or get_default_client()).call(role, system_prompt, user_prompt)
