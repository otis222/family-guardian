"""LINE 測試共用：假的傳送層、可以撥動的時鐘、webhook 事件產生器。"""

import dataclasses
from datetime import timedelta

from helpers import BASE_TIME, Scripted

from guardian.config import load_config
from guardian.data_165 import RumorIndex
from guardian.engine import DEMO_SALT, Engine
from guardian.line_bot import LineBot
from guardian.llm import LLMClient
from guardian.records import RecordStore

GROUP = "C" + "a" * 32
OTHER_GROUP = "C" + "b" * 32
GUARDIAN = "U" + "1" * 32
SECOND_GUARDIAN = "U" + "2" * 32
MEMBER = "U" + "3" * 32
STRANGER = "U" + "4" * 32
PUBLIC_URL = "https://abc.trycloudflare.com"

CANDIDATE = "訂單被誤設成分期付款，請到 ATM 操作解除分期"  # 規則判候選
WATCH = "我換新號碼了，先加這個"  # 規則判觀察區間
CHAT = "晚上吃什麼？"


class FakeTransport:
    def __init__(self, group_size=8, usage=0):
        self.replies = []
        self.pushes = []
        self.webhooks = []
        self.size = group_size
        self.usage = usage
        self.fail_reply = False
        self.fail_push = False
        self.fail_usage = False

    def reply(self, token, messages):
        if self.fail_reply:
            raise RuntimeError("回覆失敗，內容不該進日誌")
        self.replies.append((token, messages))

    def push(self, to, messages):
        if self.fail_push:
            raise RuntimeError("推播失敗，內容不該進日誌")
        self.pushes.append((to, messages))

    def group_size(self, group_id):
        return self.size

    def push_usage(self):
        if self.fail_usage:
            raise RuntimeError("查不到")
        return self.usage

    def set_webhook(self, url):
        self.webhooks.append(url)

    def texts(self):
        return [message.text for _, messages in self.replies for message in messages if hasattr(message, "text")]


class Clock:
    def __init__(self):
        self.now = BASE_TIME

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def line_config(tmp_path):
    config = load_config()
    return dataclasses.replace(config, router_latency_seconds=0, replay_cache_write=False,
                               line=dataclasses.replace(config.line, records_dir=tmp_path / "records"))


class Harness:
    """把引擎、假的傳送層和時鐘組起來。llm 省略時用 offline 假模型。"""

    def __init__(self, tmp_path, llm=None, *, mode="silent", guardians=(GUARDIAN,), config=None, public_url=PUBLIC_URL):
        self.tmp_path = tmp_path
        self.llm = llm if llm is not None else Scripted()
        self.clock = Clock()
        self.transport = FakeTransport()
        self.config = config or line_config(tmp_path)
        self.guardians = guardians
        self.public_url = public_url
        self.bot = self.new_bot()
        self.bot.engine.speaking_mode = mode
        self.sequence = 0

    def new_bot(self):
        """也用來模擬重新啟動：引擎是新的，紀錄從檔案讀回來。"""
        engine = Engine(self.config, salt=DEMO_SALT, rumors=RumorIndex(), push_recipients=len(self.guardians),
                        llm_client=LLMClient("offline", self.config.models, responder=self.llm))
        store = RecordStore(self.config.line.records_dir, self.config.record_retention_days)
        return LineBot(engine, self.transport, store, guardians=self.guardians, allowed_groups=[GROUP],
                       settings=self.config.line, public_base_url=self.public_url, now=self.clock)

    @property
    def engine(self):
        return self.bot.engine

    def token(self):
        self.sequence += 1
        return f"token-{self.sequence}"

    def timestamp(self):
        return int(self.clock.now.timestamp() * 1000)

    def group_text(self, text, *, user=MEMBER, group=GROUP, mention=False, token=None, others_mentioned=False):
        message = {"type": "text", "id": "1", "text": text}
        if mention or others_mentioned:
            message["mention"] = {"mentionees": [{"index": 0, "length": 4, "type": "user", "isSelf": mention}]}
        return {"type": "message", "timestamp": self.timestamp(), "replyToken": token or self.token(),
                "source": {"type": "group", "groupId": group, "userId": user}, "message": message}

    def direct_text(self, text, user=GUARDIAN):
        return {"type": "message", "timestamp": self.timestamp(), "replyToken": self.token(),
                "source": {"type": "user", "userId": user}, "message": {"type": "text", "id": "2", "text": text}}

    def postback(self, data, group=GROUP):
        return {"type": "postback", "timestamp": self.timestamp(), "replyToken": self.token(),
                "source": {"type": "group", "groupId": group, "userId": MEMBER}, "postback": {"data": data}}

    def send(self, *events):
        """webhook 收到事件，工作執行緒處理完。"""
        self.bot.enqueue(events)
        self.bot.process_pending()

    def command(self, text, user=GUARDIAN):
        before = len(self.transport.replies)
        self.send(self.direct_text(text, user))
        return self.transport.replies[before][1][0].text
