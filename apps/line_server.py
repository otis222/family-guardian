"""LINE webhook 伺服器（SPEC 第 10 節）。只負責 HTTP 和 line-bot-sdk 的轉換，邏輯都在 guardian/line_bot.py。

    uvicorn apps.line_server:app --port 8000
    cloudflared tunnel --url http://localhost:8000

伺服器會自己找到 Quick Tunnel 的網址，並更新 LINE 的 webhook 網址。
處理真實訊息：不寫回放快取、不用 replay（SPEC 第 8 節），沒有 GUARDIAN_HASH_SALT 就拒絕啟動（決策 D4）。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from linebot.v3 import SignatureValidator
from linebot.v3.messaging import (
    ApiClient,
    ButtonsTemplate,
    Configuration,
    ImageMessage,
    MessagingApi,
    PostbackAction,
    PushMessageRequest,
    ReplyMessageRequest,
    SetWebhookEndpointRequest,
    TemplateMessage,
    TextMessage,
)

from guardian.config import Config, load_config, load_env_file
from guardian.engine import Engine
from guardian.line_bot import ButtonOut, ImageOut, LineBot, LineEnv, StartupError, TextOut, load_line_env, logger
from guardian.llm import MODES, PROVIDER_KEY_ENV, LLMClient
from guardian.records import RecordStore
from guardian.run import never_crash_on_encoding
from guardian.tunnel import find_quick_tunnel_url
from guardian.workers.card import warning_card_png

CALLBACK_PATH = "/callback"
MODE_ENV = "LINE_LLM_MODE"
MAX_BODY_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
DEADLINE_CHECK_SECONDS = 2
TUNNEL_CHECK_SECONDS = 15
# 送出後只看 HTTP 狀態碼，不解析回應：解析失敗不代表沒送出，當成失敗會改用 push，重複發送又多花額度
SEND_OPTIONS = {"_preload_content": False, "_request_timeout": REQUEST_TIMEOUT_SECONDS}


def _to_line(message: Any) -> Any:
    if isinstance(message, TextOut):
        return TextMessage(text=message.text)
    if isinstance(message, ImageOut):
        return ImageMessage(original_content_url=message.url, preview_image_url=message.url)
    if isinstance(message, ButtonOut):
        action = PostbackAction(label=message.label, data=message.data)
        return TemplateMessage(alt_text=message.text, template=ButtonsTemplate(text=message.text, actions=[action]))
    raise TypeError(f"不認得的訊息種類：{type(message).__name__}")


class LineTransport:
    """line-bot-sdk 的轉接。失敗時丟出例外，由 LineBot 記錄，不重試。"""

    def __init__(self, access_token: str, host: str | None = None) -> None:
        self._api = MessagingApi(ApiClient(Configuration(host=host, access_token=access_token)))
        self._lock = threading.Lock()  # 工作和期限兩條執行緒都會送訊息

    def reply(self, token: str, messages: list[Any]) -> None:
        request = ReplyMessageRequest(reply_token=token, messages=[_to_line(item) for item in messages])
        with self._lock:
            self._api.reply_message_with_http_info(request, **SEND_OPTIONS)

    def push(self, to: str, messages: list[Any]) -> None:
        request = PushMessageRequest(to=to, messages=[_to_line(item) for item in messages])
        with self._lock:
            self._api.push_message_with_http_info(request, **SEND_OPTIONS)

    def group_size(self, group_id: str) -> int:
        with self._lock:
            return self._api.get_group_member_count(group_id, _request_timeout=REQUEST_TIMEOUT_SECONDS).count

    def push_usage(self) -> int:
        with self._lock:
            return self._api.get_message_quota_consumption(_request_timeout=REQUEST_TIMEOUT_SECONDS).total_usage

    def set_webhook(self, url: str) -> None:
        with self._lock:
            self._api.set_webhook_endpoint(SetWebhookEndpointRequest(endpoint=url),
                                           _request_timeout=REQUEST_TIMEOUT_SECONDS)


def prepare_config(config: Config, env: Mapping[str, str]) -> Config:
    """真實訊息不寫快取、也不用 replay（SPEC 第 8 節）。批次由真實的推理時間決定，不用模擬延遲（決策 D41）。

    模式看 LINE_LLM_MODE（決策 D56）：放在 .env 也不會影響模擬器和實驗，它們還是用回放，不會意外花 token。
    """
    mode = (env.get(MODE_ENV) or "").strip()
    if mode:
        if mode not in MODES:
            raise StartupError(f"{MODE_ENV} 必須是 live 或 offline，收到：{mode!r}")
        config = dataclasses.replace(config, llm_mode=mode)
    if config.llm_mode == "replay":
        raise StartupError(f"LINE 伺服器處理真實訊息，不能用 replay。請在 .env 設定 {MODE_ENV}=live（只測通道時可以用 offline）。")
    if config.llm_mode == "live":
        needed = {PROVIDER_KEY_ENV.get(model.split(":", 1)[0]) for model in config.models.values()} - {None}
        missing = sorted(name for name in needed if not env.get(name))
        if missing:
            raise StartupError(f".env 還沒設定：{'、'.join(missing)}")
    return dataclasses.replace(config, replay_cache_write=False, router_latency_seconds=0)


@dataclass
class Runtime:
    """一個伺服器的所有元件和背景執行緒。"""

    bot: LineBot
    transport: Any
    validator: SignatureValidator
    line_env: LineEnv
    config: Config
    tunnel_lookup: Callable[[], str | None] = find_quick_tunnel_url
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)
    _webhook_url: str | None = None

    def start(self) -> None:
        self._spawn("line-worker", self._work)
        self._spawn("line-deadlines", self._watch_deadlines)
        self._spawn("line-tunnel", self._watch_tunnel)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=REQUEST_TIMEOUT_SECONDS)

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _work(self) -> None:
        while not self._stop.is_set():
            self.bot.process_pending(timeout=self.config.line.tick_seconds)

    def _watch_deadlines(self) -> None:
        while not self._stop.wait(DEADLINE_CHECK_SECONDS):
            self.bot.check_deadlines()

    def _watch_tunnel(self) -> None:
        while True:
            self.update_webhook()
            if self._stop.wait(TUNNEL_CHECK_SECONDS):
                return

    def update_webhook(self) -> None:
        """通道網址變了就更新 LINE 的 webhook 網址。每個網址只試一次，失敗就記錄（不自動重試）。"""
        url = self.line_env.public_base_url or self.tunnel_lookup()
        if not url or url == self._webhook_url:
            return
        self._webhook_url = url
        self.bot.public_base_url = url
        try:
            self.transport.set_webhook(url + CALLBACK_PATH)
            logger.info("LINE 的 webhook 網址已更新：%s%s", url, CALLBACK_PATH)
        except Exception as exc:
            logger.error("更新 webhook 網址失敗（%s）。請到 LINE Developers 手動設定：%s%s",
                         type(exc).__name__, url, CALLBACK_PATH)


def build_runtime(env: Mapping[str, str] | None = None, *, transport: Any = None,
                  config: Config | None = None) -> Runtime:
    if env is None:
        load_env_file()
        env = os.environ
    config = prepare_config(config or load_config(env=env), env)
    line_env = load_line_env(env, config.hash_salt_env)
    client = LLMClient.from_config(config, write_cache=False)
    engine = Engine(config, salt=line_env.salt, llm_client=client, push_recipients=len(line_env.guardians))
    transport = transport or LineTransport(line_env.access_token)
    bot = LineBot(engine, transport, RecordStore(config.line.records_dir, config.record_retention_days),
                  guardians=line_env.guardians, allowed_groups=line_env.allowed_groups, settings=config.line,
                  public_base_url=line_env.public_base_url)
    try:
        engine.watchdog.sync_push_usage(transport.push_usage())
    except Exception as exc:
        logger.error("查詢推播用量失敗：%s，先從 0 開始算", type(exc).__name__)
    logger.info("顧家貓啟動：模型 %s，發言模式 %s%s，守護人 %d 位，允許的群組 %d 個",
                config.llm_mode, engine.speaking_mode, "（暫停中）" if bot.paused else "",
                len(line_env.guardians), len(line_env.allowed_groups))
    if config.llm_mode == "live":
        logger.info("提醒：處理家人訊息前，請確認 Groq 的 Data Controls 已開啟 Zero Data Retention（SPEC 第 8 節）。")
    if config.llm_mode == "offline":
        logger.warning("目前是 offline 假模型，只能用來測通道，判斷結果不是真的。")
    return Runtime(bot, transport, SignatureValidator(line_env.channel_secret), line_env, config)


def _setup_logging() -> None:
    never_crash_on_encoding()
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False


def create_app(build: Callable[[], Runtime] = build_runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _setup_logging()
        try:
            runtime = build()
        except StartupError as exc:
            print(f"無法啟動：{exc}", file=sys.stderr)
            raise
        app.state.runtime = runtime
        runtime.start()
        yield
        runtime.stop()

    # 這個網址會經過通道公開出去，所以不開放說明文件
    app = FastAPI(title="顧家貓", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post(CALLBACK_PATH)
    async def callback(request: Request) -> dict[str, str]:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="too large")
        runtime: Runtime = request.app.state.runtime
        signature = request.headers.get("X-Line-Signature", "")
        try:
            text = body.decode("utf-8")
            valid = bool(signature) and runtime.validator.validate(text, signature)
        except (UnicodeDecodeError, ValueError):
            valid = False
        if not valid:
            raise HTTPException(status_code=400, detail="invalid signature")
        try:
            events = json.loads(text).get("events") or []
        except (json.JSONDecodeError, AttributeError):
            raise HTTPException(status_code=400, detail="invalid body") from None
        if isinstance(events, list):
            runtime.bot.enqueue(events)
        return {"status": "ok"}

    @app.get("/card/warning.png")
    def warning_card() -> Response:
        return Response(warning_card_png(), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
