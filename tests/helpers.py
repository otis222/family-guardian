"""測試共用：可以照腳本回應的假模型、假的 aisuite、固定分數的快速過濾、事件和劇本的產生器。"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from guardian.data_165 import RumorIndex
from guardian.engine import DEMO_SALT, Engine
from guardian.llm import LLMClient, offline_responder
from guardian.models import RiskSignal
from guardian.prompts import ROUTER_SYSTEM, SINGLE_SYSTEM, SPEAKER_SYSTEM, VERIFIER_SYSTEM
from guardian.scenario import Member, Scenario, ScenarioMessage

BASE_TIME = datetime(2026, 11, 8, 8, 0, tzinfo=timezone(timedelta(hours=8)))
RULE = "測試規則"


def verify_json(level, reasons=("有詐騙常見的特徵",)):
    return json.dumps({"risk_level": level, "reasons": list(reasons), "evidence": [], "advice": ["打 165"]},
                      ensure_ascii=False)


def route_all(action):
    """Router 的回應：批次裡每個事件都回同一個動作。"""
    def reply(user_prompt):
        events = json.loads(user_prompt)["events"]
        return json.dumps({"decisions": [{"event_id": e["event_id"], "action": action, "reason": "測試"}
                                         for e in events]})
    return reply


class Scripted:
    """依角色回應。沒指定的角色用 offline 假模型；值可以是文字、函式、例外，或依序使用的清單。"""

    def __init__(self, **by_role):
        self.by_role = by_role
        self.calls = []  # (role, system_prompt, user_prompt)

    def __call__(self, role, system_prompt, user_prompt):
        self.calls.append((role, system_prompt, user_prompt))
        reply = self.by_role.get(role)
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            return offline_responder(role, system_prompt, user_prompt)
        return reply(user_prompt) if callable(reply) else reply

    def count(self, role, task=None):
        return sum(1 for r, _, u in self.calls
                   if r == role and (task is None or json.loads(u).get("task") == task))

    def batches(self):
        """每次 Router 分流呼叫裡的事件編號。"""
        return [[e["event_id"] for e in json.loads(u)["events"]]
                for r, _, u in self.calls if r == "router" and json.loads(u).get("task") == "route"]


class RecordingAisuite:
    """長得像 aisuite.Client，回應內容由 offline 假模型產生。fail_after：成功幾次之後開始失敗。"""

    ROLES = {ROUTER_SYSTEM: "router", SPEAKER_SYSTEM: "router", VERIFIER_SYSTEM: "verifier", SINGLE_SYSTEM: "verifier"}

    def __init__(self, fail_after=None):
        self.chat = SimpleNamespace(completions=self)
        self.calls = 0
        self.fail_after = fail_after

    def create(self, model, messages, **kwargs):
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise RuntimeError("429 rate limit")
        self.calls += 1
        system, user = messages[0]["content"], messages[1]["content"]
        content = offline_responder(self.ROLES.get(system, "reporter"), system, user)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


def fixed_filter(scores):
    """事件編號 → 分數；不在表裡的是 0 分。"""
    return lambda trigger: RiskSignal(scores.get(trigger.id, 0), [RULE] if scores.get(trigger.id) else [])


def make_engine(config, llm=None, *, scores=None, **kwargs):
    llm = llm if llm is not None else Scripted()
    client = LLMClient("offline", config.models, responder=llm)
    kwargs.setdefault("rumors", RumorIndex())
    if scores is not None:
        kwargs["fast_filter"] = fixed_filter(scores)
    return Engine(config, salt=DEMO_SALT, llm_client=client, **kwargs)


def event(event_id, seconds=0, text="早安", user="u1", group="g", mention=False):
    return {"id": event_id, "time": BASE_TIME + timedelta(seconds=seconds), "source": "group", "group_id": group,
            "user_id": user, "kind": "text", "text": text, "mention": mention}


def record_of(engine, event_id):
    return next((r for r in engine.records if r.event_id == event_id), None)


def message(message_id, seconds=0, label="normal", text="早安", mention=False, compare=False, kind="text"):
    return ScenarioMessage(id=message_id, time=BASE_TIME + timedelta(seconds=seconds), sender="u1", kind=kind,
                           text=text, mention=mention, label=label, technique="手法" if label != "normal" else None,
                           compare=compare)


def scenario_of(*messages, blocklist=()):
    return Scenario(name="測試劇本", description="", group_id="g", members=(Member("u1", "甲"),),
                    messages=tuple(messages), blocklist=tuple(blocklist))
