"""call_llm 的三種模式（SPEC 第 8 節；測試清單第 12 項：回放模式不需金鑰、結果和錄製時一致）。"""

import http.server
import json
import threading
from types import SimpleNamespace

import pytest

from guardian.llm import (
    LLMCallError,
    LLMClient,
    LLMError,
    LLMUnusableReplyError,
    ReplayMissError,
    cache_key,
    call_llm,
    extract_json,
    safe_error,
)
from guardian.models import SchemaError, VerifyResult, parse_decisions

MODELS = {role: "groq:openai/gpt-oss-120b" for role in ("router", "verifier", "reporter")}
FAKE_KEY = "gsk_" + "x" * 24
VERIFY_JSON = '{"risk_level": "medium", "reasons": ["要求轉帳"], "evidence": [], "advice": ["打 165"]}'


class FakeCompletions:
    def __init__(self, content=VERIFY_JSON, exc=None, usage=(12, 5)):
        self.content, self.exc, self.usage = content, exc, usage
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        usage = SimpleNamespace(prompt_tokens=self.usage[0], completion_tokens=self.usage[1]) if self.usage else None
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeAisuite:
    """長得像 aisuite.Client 的假物件。"""

    def __init__(self, **kwargs):
        self.completions = FakeCompletions(**kwargs)
        self.chat = SimpleNamespace(completions=self.completions)


class ExplodingClient:
    """一被用到就失敗：用來確認 replay 不會偷偷連網。"""

    @property
    def chat(self):
        raise AssertionError("不應該呼叫 live client")


def live_client(tmp_path, fake, **kwargs):
    return LLMClient("live", MODELS, cache_dir=tmp_path, live_client=fake, env={"GROQ_API_KEY": FAKE_KEY}, **kwargs)


# ---------------------------------------------------------------- offline

def test_offline_router_decisions_follow_schema():
    user = json.dumps([{"event_id": "m03", "text": "..."}, {"event_id": "m06", "text": "..."}], ensure_ascii=False)
    decisions = parse_decisions(extract_json(LLMClient("offline", MODELS).call("router", "系統", user)))
    assert [d.event_id for d in decisions] == ["m03", "m06"]


def test_offline_verifier_follows_schema():
    result = VerifyResult.from_dict(extract_json(LLMClient("offline", MODELS).call("verifier", "s", "u")))
    assert result.risk_level in ("low", "medium", "high")


def test_offline_reporter_returns_text():
    assert LLMClient("offline", MODELS).call("reporter", "s", "u").strip()


def test_offline_needs_no_key_or_cache_dir():
    client = LLMClient("offline", MODELS, env={})
    assert client.call("reporter", "s", "u")


def test_unknown_role_is_rejected():
    client = LLMClient("offline", MODELS)
    with pytest.raises(ValueError):
        client.call("painter", "s", "u")
    assert client.stats.total_calls == 0


def test_injected_responder_and_call_counts():
    seen = []

    def responder(role, system_prompt, user_prompt):
        seen.append(role)
        return VERIFY_JSON

    client = LLMClient("offline", MODELS, responder=responder)
    client.call("verifier", "s", "u1")
    client.call("verifier", "s", "u2")
    client.call("reporter", "s", "u3")
    assert seen == ["verifier", "verifier", "reporter"]
    assert client.stats.as_dict()["calls"] == {"router": 0, "verifier": 2, "reporter": 1}
    assert client.stats.total_calls == 3


def test_blank_reply_counts_as_error():
    client = LLMClient("offline", MODELS, responder=lambda *args: "   ")
    with pytest.raises(LLMCallError):
        client.call("reporter", "s", "u")
    assert client.stats.errors["reporter"] == 1


# ---------------------------------------------------------------- replay

def test_replay_miss_raises_and_never_goes_live(tmp_path):
    client = LLMClient("replay", MODELS, cache_dir=tmp_path, live_client=ExplodingClient(), env={})
    with pytest.raises(ReplayMissError):
        client.call("router", "s", "u")
    assert client.stats.errors["router"] == 1


def test_live_records_then_replay_matches_without_key(tmp_path):
    fake = FakeAisuite()
    recorded = live_client(tmp_path, fake).call("verifier", "系統提示", "使用者內容：測試訊息")

    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1
    saved = files[0].read_text(encoding="utf-8")
    assert "系統提示" not in saved and "使用者內容" not in saved  # 快取不存 prompt 原文
    assert FAKE_KEY not in saved

    replay = LLMClient("replay", MODELS, cache_dir=tmp_path, live_client=ExplodingClient(), env={})
    assert replay.call("verifier", "系統提示", "使用者內容：測試訊息") == recorded
    assert replay.stats.cache_hits == 1
    assert (replay.stats.prompt_tokens, replay.stats.completion_tokens) == (12, 5)  # token 用量也能重現


def test_replay_misses_when_prompt_changes(tmp_path):
    live_client(tmp_path, FakeAisuite()).call("verifier", "s", "原本的內容")
    replay = LLMClient("replay", MODELS, cache_dir=tmp_path, env={})
    with pytest.raises(ReplayMissError):
        replay.call("verifier", "s", "改過的內容")


def test_replay_rejects_tampered_cache(tmp_path):
    live_client(tmp_path, FakeAisuite()).call("verifier", "s", "u")
    path = next(tmp_path.rglob("*.json"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["key"] = "0" * 64
    path.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(ReplayMissError):
        LLMClient("replay", MODELS, cache_dir=tmp_path, env={}).call("verifier", "s", "u")


# ---------------------------------------------------------------- live

def test_live_passes_messages_and_params(tmp_path):
    fake = FakeAisuite(content="好")
    client = live_client(tmp_path, fake, params={"reporter": {"temperature": 0.3, "reasoning_effort": "low"}})
    client.call("reporter", "S", "U")
    call = fake.completions.calls[0]
    assert call["model"] == MODELS["reporter"]
    assert call["messages"] == [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    assert (call["temperature"], call["reasoning_effort"]) == (0.3, "low")


def test_live_without_key_fails_before_calling(tmp_path):
    fake = FakeAisuite()
    client = LLMClient("live", MODELS, cache_dir=tmp_path, live_client=fake, env={})
    with pytest.raises(LLMCallError, match="GROQ_API_KEY"):
        client.call("router", "s", "u")
    assert fake.completions.calls == []


def test_live_error_is_masked_and_not_retried(tmp_path):
    fake = FakeAisuite(exc=RuntimeError(f"Invalid API Key: {FAKE_KEY}"))
    client = live_client(tmp_path, fake)
    with pytest.raises(LLMCallError) as info:
        client.call("router", "s", "u")
    assert FAKE_KEY not in str(info.value)
    assert len(fake.completions.calls) == 1  # 不自動重試
    assert client.stats.errors["router"] == 1
    assert not list(tmp_path.rglob("*.json"))  # 失敗不寫快取


def test_live_can_skip_the_cache_for_real_messages(tmp_path):
    # 決策 D2：家庭試行處理真實訊息時不寫回放快取
    client = live_client(tmp_path, FakeAisuite(), write_cache=False)
    assert client.call("verifier", "s", "u")
    assert not list(tmp_path.rglob("*.json"))
    assert client.stats.cache_writes == 0


def test_live_blank_reply_is_an_error_and_not_cached(tmp_path):
    client = live_client(tmp_path, FakeAisuite(content="  "))
    with pytest.raises(LLMCallError):
        client.call("verifier", "s", "u")
    assert not list(tmp_path.rglob("*.json"))


# ---------------------------------------------------------------- 工具函式

def test_safe_error_masks_keys_and_env_values():
    secret = "line-token-123456"
    raw = f"bad {FAKE_KEY} sk-abcdefghijk1234 AIza{'A' * 30} Bearer abc.def.ghi1234 {secret}"
    text = safe_error(RuntimeError(raw), env={"LINE_CHANNEL_ACCESS_TOKEN": secret})
    for leaked in (FAKE_KEY, "sk-abcdefghijk1234", "AIza" + "A" * 30, "abc.def.ghi1234", secret):
        assert leaked not in text


def test_safe_error_truncates():
    assert len(safe_error("x" * 1000, limit=50, env={})) == 50


@pytest.mark.parametrize("text", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    '結果如下：{"a": 1}，以上。',
])
def test_extract_json_variants(text):
    assert extract_json(text) == {"a": 1}


@pytest.mark.parametrize("text", ["沒有 JSON", "", "{壞掉的"])
def test_extract_json_rejects_garbage(text):
    with pytest.raises(SchemaError):
        extract_json(text)


def test_cache_key_depends_on_every_part():
    base = cache_key("router", "m", "s", "u")
    assert base == cache_key("router", "m", "s", "u")
    others = {cache_key("verifier", "m", "s", "u"), cache_key("router", "m2", "s", "u"),
              cache_key("router", "m", "s2", "u"), cache_key("router", "m", "s", "u2")}
    assert base not in others and len(others) == 4


def test_call_llm_default_client_follows_env():
    # conftest 把 GUARDIAN_LLM_MODE 設成 offline
    assert call_llm("reporter", "s", "u").startswith("（offline")


def test_call_llm_with_explicit_client():
    client = LLMClient("offline", MODELS)
    call_llm("reporter", "s", "u", client=client)
    assert client.stats.calls["reporter"] == 1


# ---------------------------------------------------------------- 真的 aisuite 和 groq SDK，連到本機的替身

class FakeGroqApi(http.server.BaseHTTPRequestHandler):
    requests = []
    status = 200

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        type(self).requests.append((self.path, body))
        if type(self).status == 200:
            payload = {"id": "c1", "object": "chat.completion", "created": 1, "model": body["model"],
                       "choices": [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": "回覆內容"}}],
                       "usage": {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168}}
        elif type(self).status == 400:
            payload = {"error": {"message": "Tool choice is none, but model called a tool", "code": "tool_use_failed",
                                 "failed_generation": "模型的輸出，可能引用訊息原文"}}
        else:
            payload = {"error": {"message": "rate limit", "type": "tokens", "code": "rate_limit_exceeded"}}
        data = json.dumps(payload).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def groq_api(monkeypatch):
    FakeGroqApi.requests = []
    FakeGroqApi.status = 200
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGroqApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GROQ_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    yield FakeGroqApi
    server.shutdown()
    thread.join(timeout=5)


def test_real_aisuite_sends_our_parameters_and_keeps_the_token_counts(groq_api, tmp_path):
    client = LLMClient("live", MODELS, cache_dir=tmp_path,
                       params={"router": {"temperature": 0.3, "reasoning_effort": "low", "max_completion_tokens": 8192}})
    assert client.call("router", "系統提示", "使用者內容") == "回覆內容"
    path, body = groq_api.requests[0]
    assert path == "/openai/v1/chat/completions"
    assert body["model"] == MODELS["router"].split(":", 1)[1]
    assert (body["temperature"], body["reasoning_effort"], body["max_completion_tokens"]) == (0.3, "low", 8192)
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert (client.stats.prompt_tokens, client.stats.completion_tokens) == (123, 45)
    assert client.stats.cache_writes == 1


@pytest.mark.parametrize("status", [429, 500])
def test_real_sdk_does_not_retry_on_its_own(groq_api, tmp_path, status):
    groq_api.status = status
    client = LLMClient("live", MODELS, cache_dir=tmp_path)
    with pytest.raises(LLMError) as error:
        client.call("router", "系統提示", "使用者內容")
    assert len(groq_api.requests) == 1  # SDK 預設會再試兩次
    assert FAKE_KEY not in str(error.value)
    assert client.stats.total_errors == 1 and not list(tmp_path.rglob("*.json"))


def test_model_trying_to_call_a_tool_is_an_unusable_reply_and_hides_its_output(groq_api, tmp_path):
    groq_api.status = 400
    client = LLMClient("live", MODELS, cache_dir=tmp_path)
    with pytest.raises(LLMUnusableReplyError) as error:
        client.call("verifier", "系統提示", "使用者內容")
    assert "訊息原文" not in str(error.value)
    assert client.stats.total_errors == 1
