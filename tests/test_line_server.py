"""LINE webhook 伺服器（SPEC 第 10 節、第 13 節第 11 項）。不連外部：LINE API 用本機的替身。"""

import base64
import dataclasses
import hashlib
import hmac
import http.server
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from helpers import RecordingAisuite
from line_helpers import CANDIDATE, CHAT, GROUP, GUARDIAN, OTHER_GROUP, FakeTransport, line_config

from apps import line_server
from apps.line_server import LineTransport, Runtime, build_runtime, create_app, prepare_config
from guardian.engine import DEMO_SALT
from guardian.line_bot import ButtonOut, ImageOut, StartupError, TextOut, load_line_env
from guardian.tunnel import find_quick_tunnel_url

SECRET = "test-channel-secret"
ENV = {
    "LINE_CHANNEL_SECRET": SECRET,
    "LINE_CHANNEL_ACCESS_TOKEN": "test-access-token",
    "GUARDIAN_HASH_SALT": "test-salt",
    "GUARDIAN_LINE_USER_IDS": GUARDIAN,
    "GUARDIAN_GROUP_IDS": GROUP,
}


def sign(body, secret=SECRET):
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def webhook_body(*events):
    return json.dumps({"destination": "U0", "events": list(events)}, ensure_ascii=False).encode("utf-8")


def text_event(text=CANDIDATE, token="token-1"):
    return {"type": "message", "timestamp": 1_794_000_000_000, "replyToken": token,
            "source": {"type": "group", "groupId": GROUP, "userId": "U" + "3" * 32},
            "message": {"type": "text", "id": "1", "text": text}}


@pytest.fixture
def config(tmp_path):
    return line_config(tmp_path)


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def runtime(config, transport):
    built = build_runtime(ENV, transport=transport, config=config)
    built.tunnel_lookup = lambda: None
    return built


@pytest.fixture
def client(runtime):
    """不啟動背景執行緒，測試自己決定什麼時候處理佇列。"""
    runtime.start = lambda: None
    runtime.stop = lambda: None
    with TestClient(create_app(lambda: runtime)) as test_client:
        yield test_client


def post(client, body, signature=None, **headers):
    if signature is not None:
        headers["X-Line-Signature"] = signature
    return client.post("/callback", content=body, headers=headers)


# ---------------------------------------------------------------- 第 11 項：簽章驗證

def test_correct_signature_is_accepted_and_the_event_is_processed(client, runtime, transport):
    body = webhook_body(text_event())
    assert post(client, body, sign(body)).status_code == 200
    assert transport.pushes == []  # 回 200 的時候還沒處理，webhook 不等模型
    runtime.bot.process_pending()
    assert len(runtime.bot.engine.records) == 1 and [to for to, _ in transport.pushes] == [GUARDIAN]


@pytest.mark.parametrize("signature", ["", "bm90LXRoZS1zaWduYXR1cmU=", "不是簽章".encode("utf-8"), sign(b"other body")])
def test_wrong_signature_is_rejected_and_nothing_happens(client, runtime, signature):
    body = webhook_body(text_event())
    assert post(client, body, signature).status_code == 400
    runtime.bot.process_pending()
    assert runtime.bot.engine.counts["events"] == 0


def test_missing_signature_is_rejected(client, runtime):
    assert post(client, webhook_body(text_event())).status_code == 400
    runtime.bot.process_pending()
    assert runtime.bot.engine.counts["events"] == 0


def test_signature_from_another_secret_is_rejected(client):
    body = webhook_body(text_event())
    assert post(client, body, sign(body, "someone-else")).status_code == 400


def test_tampered_body_is_rejected(client):
    body = webhook_body(text_event())
    signature = sign(body)
    assert post(client, webhook_body(text_event("改過的內容")), signature).status_code == 400


def test_non_utf8_body_is_rejected(client):
    body = b"\xff\xfe\x00"
    assert post(client, body, sign(body)).status_code == 400


def test_line_verify_button_gets_a_200(client):
    body = webhook_body()
    assert post(client, body, sign(body)).status_code == 200


def test_signed_but_broken_json_is_rejected(client):
    for body in (b"not json", b"[1, 2]"):
        assert post(client, body, sign(body)).status_code == 400


def test_huge_body_is_rejected_before_doing_anything(client):
    body = b" " * (line_server.MAX_BODY_BYTES + 1)
    assert post(client, body, sign(body)).status_code == 413


# ---------------------------------------------------------------- 其他網址

def test_card_is_served_as_a_png(client):
    response = client.get("/card/warning.png")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_health_check_and_no_public_docs(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ---------------------------------------------------------------- 啟動檢查

@pytest.mark.parametrize("missing", ["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "GUARDIAN_HASH_SALT",
                                     "GUARDIAN_LINE_USER_IDS"])
def test_refuses_to_start_without_required_settings(config, transport, missing):
    env = {key: value for key, value in ENV.items() if key != missing}
    with pytest.raises(StartupError, match=missing) as error:
        build_runtime(env, transport=transport, config=config)
    assert "test-access-token" not in str(error.value) and "test-salt" not in str(error.value)


def test_blank_salt_is_the_same_as_no_salt(config, transport):
    with pytest.raises(StartupError, match="GUARDIAN_HASH_SALT"):
        build_runtime({**ENV, "GUARDIAN_HASH_SALT": "   "}, transport=transport, config=config)


@pytest.mark.parametrize("name,value", [("GUARDIAN_LINE_USER_IDS", "not-an-id"), ("GUARDIAN_GROUP_IDS", "U123"),
                                        ("PUBLIC_BASE_URL", "http://insecure.example")])
def test_bad_settings_are_refused_with_a_clear_message(config, transport, name, value):
    with pytest.raises(StartupError, match=name):
        build_runtime({**ENV, name: value}, transport=transport, config=config)


def test_several_guardians_and_groups_are_accepted():
    second = "U" + "2" * 32
    parsed = load_line_env({**ENV, "GUARDIAN_LINE_USER_IDS": f"{GUARDIAN}, {second}",
                            "GUARDIAN_GROUP_IDS": f"{GROUP},{OTHER_GROUP}"}, "GUARDIAN_HASH_SALT")
    assert parsed.guardians == (GUARDIAN, second) and parsed.allowed_groups == {GROUP, OTHER_GROUP}


def test_no_allowed_group_is_fine_at_first(config, transport):
    env = {key: value for key, value in ENV.items() if key != "GUARDIAN_GROUP_IDS"}
    assert build_runtime(env, transport=transport, config=config).bot.allowed_groups == frozenset()


def test_replay_mode_is_refused_for_real_messages(config, transport):
    with pytest.raises(StartupError, match="replay"):
        build_runtime(ENV, transport=transport, config=dataclasses.replace(config, llm_mode="replay"))


def test_line_mode_setting_only_affects_the_server(config):
    replay = dataclasses.replace(config, llm_mode="replay")
    assert prepare_config(replay, {"LINE_LLM_MODE": "offline"}).llm_mode == "offline"
    assert replay.llm_mode == "replay"  # 模擬器和實驗還是用回放，放在 .env 也不會意外花 token
    with pytest.raises(StartupError, match="GROQ_API_KEY"):
        prepare_config(replay, {"LINE_LLM_MODE": "live"})
    assert prepare_config(replay, {"LINE_LLM_MODE": "live", "GROQ_API_KEY": "gsk_" + "x" * 24}).llm_mode == "live"


@pytest.mark.parametrize("mode", ["replay", "online"])
def test_line_mode_setting_refuses_anything_else(config, mode):
    with pytest.raises(StartupError):
        prepare_config(dataclasses.replace(config, llm_mode="replay"), {"LINE_LLM_MODE": mode})


def test_live_mode_needs_the_model_key(config, transport):
    live = dataclasses.replace(config, llm_mode="live")
    with pytest.raises(StartupError, match="GROQ_API_KEY"):
        build_runtime(ENV, transport=transport, config=live)
    prepared = prepare_config(live, {"GROQ_API_KEY": "gsk_" + "x" * 24})
    assert prepared.llm_mode == "live"


def test_cache_writing_and_simulated_latency_are_forced_off(config):
    prepared = prepare_config(dataclasses.replace(config, replay_cache_write=True, router_latency_seconds=20), {})
    assert prepared.replay_cache_write is False and prepared.router_latency_seconds == 0


def test_live_mode_never_writes_real_messages_to_the_replay_cache(config, transport, tmp_path):
    settings = dataclasses.replace(config, llm_mode="live", replay_cache_write=True, replay_cache_dir=tmp_path / "cache")
    built = build_runtime({**ENV, "GROQ_API_KEY": "gsk_" + "x" * 24}, transport=transport, config=settings)
    client = built.bot.engine.llm
    client._live_client = RecordingAisuite()
    client._env = {"GROQ_API_KEY": "gsk_" + "x" * 24}
    built.bot.enqueue([text_event()])
    built.bot.process_pending()
    assert client._live_client.calls > 0
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").rglob("*.json"))


def test_real_salt_is_used_not_the_demo_salt(runtime):
    assert runtime.bot.engine._salt == "test-salt" != DEMO_SALT


def test_every_guardian_is_counted_for_push_quota(config, transport):
    second = "U" + "2" * 32
    built = build_runtime({**ENV, "GUARDIAN_LINE_USER_IDS": f"{GUARDIAN},{second}"}, transport=transport, config=config)
    assert built.bot.engine.push_recipients == 2


def test_push_usage_is_read_from_line_at_startup(config, transport):
    transport.usage = 42
    assert build_runtime(ENV, transport=transport, config=config).bot.engine.watchdog.push_used == 42


def test_failed_usage_lookup_does_not_block_startup(config, transport):
    transport.fail_usage = True
    assert build_runtime(ENV, transport=transport, config=config).bot.engine.watchdog.push_used == 0


def test_startup_error_stops_the_app_with_the_reason(capsys):
    def refuse():
        raise StartupError(".env 還沒設定：GUARDIAN_HASH_SALT")

    with pytest.raises(StartupError), TestClient(create_app(refuse)):
        pass
    assert "無法啟動" in capsys.readouterr().err


# ---------------------------------------------------------------- Webhook 網址

def test_tunnel_url_updates_the_webhook_once_per_address(runtime, transport):
    addresses = ["https://first.trycloudflare.com", "https://first.trycloudflare.com",
                 "https://second.trycloudflare.com"]
    runtime.tunnel_lookup = lambda: addresses.pop(0)
    for _ in range(3):
        runtime.update_webhook()
    assert transport.webhooks == ["https://first.trycloudflare.com/callback", "https://second.trycloudflare.com/callback"]
    assert runtime.bot.public_base_url == "https://second.trycloudflare.com"


def test_waiting_for_the_tunnel_does_nothing(runtime, transport):
    runtime.update_webhook()
    assert transport.webhooks == [] and runtime.bot.public_base_url is None


def test_fixed_public_url_wins_over_the_tunnel(config, transport):
    built = build_runtime({**ENV, "PUBLIC_BASE_URL": "https://bot.example.com/"}, transport=transport, config=config)
    built.tunnel_lookup = lambda: "https://other.trycloudflare.com"
    built.update_webhook()
    assert transport.webhooks == ["https://bot.example.com/callback"]


def test_failed_webhook_update_is_tried_once_and_says_what_to_do(runtime, transport, caplog):
    def broken(url):
        raise RuntimeError("401")

    transport.set_webhook = broken
    calls = []
    runtime.tunnel_lookup = lambda: calls.append(1) or "https://first.trycloudflare.com"
    with caplog.at_level("ERROR", logger="guardian.line"):
        runtime.update_webhook()
        runtime.update_webhook()
    assert caplog.text.count("手動設定") == 1
    assert runtime.bot.public_base_url == "https://first.trycloudflare.com"  # 圖卡網址還是可以用


def test_quick_tunnel_address_is_found_on_any_metrics_port():
    def fetch(url):
        if ":20243/" in url:
            return b'{"hostname":"abc-def.trycloudflare.com"}'
        raise OSError("沒有服務")

    assert find_quick_tunnel_url(fetch) == "https://abc-def.trycloudflare.com"


@pytest.mark.parametrize("reply", [b'{"hostname":""}', b'{"hostname":"evil.example.com"}', b"not json", b"[1]"])
def test_named_tunnels_and_garbage_are_not_mistaken_for_a_quick_tunnel(reply):
    assert find_quick_tunnel_url(lambda url: reply) is None


def test_no_tunnel_running():
    def fetch(url):
        raise OSError("沒有服務")

    assert find_quick_tunnel_url(fetch) is None


# ---------------------------------------------------------------- 背景執行緒

def wait_until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_background_threads_handle_a_webhook_end_to_end(config, transport):
    fast = dataclasses.replace(config, line=dataclasses.replace(config.line, tick_seconds=1))
    built = build_runtime(ENV, transport=transport, config=fast)
    built.tunnel_lookup = lambda: "https://first.trycloudflare.com"
    with TestClient(create_app(lambda: built)) as test_client:
        body = webhook_body(text_event())
        assert post(test_client, body, sign(body)).status_code == 200
        assert wait_until(lambda: transport.pushes and transport.webhooks)
    assert not any(thread.is_alive() for thread in built._threads)
    assert [to for to, _ in transport.pushes] == [GUARDIAN]


def test_answered_chat_mention_runs_through_the_real_worker(config, transport):
    fast = dataclasses.replace(config, line=dataclasses.replace(config.line, tick_seconds=1))
    built = build_runtime(ENV, transport=transport, config=fast)
    built.tunnel_lookup = lambda: None
    event = text_event(CHAT)
    event["message"]["mention"] = {"mentionees": [{"type": "user", "isSelf": True}]}
    with TestClient(create_app(lambda: built)) as test_client:
        body = webhook_body(event)
        post(test_client, body, sign(body))
        assert wait_until(lambda: transport.replies)
    assert transport.replies[0][0] == "token-1"


# ---------------------------------------------------------------- 真的 line-bot-sdk，連到本機的替身

class FakeLineApi(http.server.BaseHTTPRequestHandler):
    requests = []
    status = 200

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"null")
        type(self).requests.append((self.command, self.path, self.headers.get("Authorization"), body))
        replies = {"/members/count": {"count": 7}, "/quota/consumption": {"totalUsage": 12}}
        payload = next((value for suffix, value in replies.items() if self.path.endswith(suffix)), {})
        data = json.dumps(payload).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def line_api():
    FakeLineApi.requests = []
    FakeLineApi.status = 200
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLineApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield FakeLineApi, LineTransport("the-token", host=f"http://127.0.0.1:{server.server_port}")
    server.shutdown()
    thread.join(timeout=5)


def test_reply_request_matches_the_messaging_api(line_api):
    api, transport = line_api
    transport.reply("reply-token", [TextOut("提醒文字"), ImageOut("https://x.trycloudflare.com/card/warning.png")])
    method, path, auth, body = api.requests[0]
    assert (method, path, auth) == ("POST", "/v2/bot/message/reply", "Bearer the-token")
    assert body["replyToken"] == "reply-token"
    assert body["messages"][0] == {"type": "text", "text": "提醒文字"}
    assert body["messages"][1] == {"type": "image",
                                   "originalContentUrl": "https://x.trycloudflare.com/card/warning.png",
                                   "previewImageUrl": "https://x.trycloudflare.com/card/warning.png"}


def test_button_request_matches_the_messaging_api(line_api):
    api, transport = line_api
    transport.reply("reply-token", [ButtonOut("還在查證", "看結果", "result:1108-103502")])
    message = api.requests[0][3]["messages"][0]
    assert message["type"] == "template" and message["altText"] == "還在查證"
    template = message["template"]
    assert template["type"] == "buttons" and template["text"] == "還在查證"
    assert template["actions"] == [{"type": "postback", "label": "看結果", "data": "result:1108-103502"}]


def test_push_and_lookups_match_the_messaging_api(line_api):
    api, transport = line_api
    transport.push(GUARDIAN, [TextOut("報告")])
    assert transport.group_size(GROUP) == 7
    assert transport.push_usage() == 12
    transport.set_webhook("https://x.trycloudflare.com/callback")
    (push, size, usage, webhook) = api.requests
    assert push[:2] == ("POST", "/v2/bot/message/push") and push[3]["to"] == GUARDIAN
    assert size[:2] == ("GET", f"/v2/bot/group/{GROUP}/members/count")
    assert usage[:2] == ("GET", "/v2/bot/message/quota/consumption")
    assert webhook[:2] == ("PUT", "/v2/bot/channel/webhook/endpoint")
    assert webhook[3] == {"endpoint": "https://x.trycloudflare.com/callback"}


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_line_error_raises_without_a_retry(line_api, status):
    api, transport = line_api
    api.status = status
    with pytest.raises(Exception):
        transport.reply("reply-token", [TextOut("提醒文字")])
    with pytest.raises(Exception):
        transport.push(GUARDIAN, [TextOut("報告")])
    assert len(api.requests) == 2


def test_a_sent_message_counts_as_sent_even_if_the_response_is_unexpected(line_api):
    # 替身回的是空物件，真的 SDK 解析會失敗。當成失敗的話會改用 push，重複發送又多花額度
    api, transport = line_api
    transport.reply("reply-token", [TextOut("提醒文字")])
    transport.push(GUARDIAN, [TextOut("報告")])
    assert len(api.requests) == 2


def test_unknown_message_type_is_a_programming_error():
    with pytest.raises(TypeError):
        line_server._to_line(object())


def test_module_level_app_exists_for_uvicorn():
    assert line_server.app.title == "顧家貓"
    assert isinstance(line_server.Runtime, type) and Runtime is line_server.Runtime
