"""版本化业务工作流：知识单元、带时区事件与快照冻结。"""
import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

TRANSITIONS = {
    "submit_review": ("draft", "reviewing"),
    "approve": ("reviewing", "approved"),
    "reject": ("reviewing", "rejected"),
    "revise": ("rejected", "draft"),
    "cancel": ("draft", "cancelled"),
}


class DomainError(ValueError):
    """业务规则拒绝，携带可解释信息。"""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def payload(self):
        return {"error": self.code, "message": self.message, **self.extra}


def parse_event_time(value):
    """解析带时区的事件时间并归一化为 UTC；朴素时间戳一律拒绝。"""
    if not isinstance(value, str) or not value.strip():
        raise DomainError("invalid_input", "occurred_at 必须是 ISO-8601 字符串")
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise DomainError("invalid_input", f"occurred_at 无法解析为 ISO-8601 时间: {value!r}")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise DomainError("invalid_input", "occurred_at 缺少时区偏移，示例: 2026-09-26T10:00:00+08:00")
    return moment.astimezone(timezone.utc)


def iso(moment):
    """UTC ISO 字符串，统一以 Z 结尾。"""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    actor: str
    occurred_at: str  # UTC ISO 字符串


@dataclass(frozen=True)
class Unit:
    id: str
    title: str
    disciplines: tuple
    state: str = "draft"
    version: int = 1
    events: tuple = ()
    snapshot: dict = None

    def view(self):
        return {
            "id": self.id, "title": self.title, "disciplines": list(self.disciplines),
            "state": self.state, "version": self.version,
            "events": [asdict(e) for e in self.events],
            "frozen": self.snapshot is not None, "snapshot": self.snapshot,
        }

    def apply(self, etype, actor, occurred_at):
        if self.snapshot is not None:
            raise DomainError("frozen", f"知识单元 {self.id} 已冻结，拒绝事件 {etype}", state=self.state)
        if etype not in TRANSITIONS:
            raise DomainError("invalid_input", f"未知事件类型: {etype}", allowed=sorted(TRANSITIONS))
        src, dst = TRANSITIONS[etype]
        if self.state != src:
            allowed = sorted(t for t, (s, _) in TRANSITIONS.items() if s == self.state)
            raise DomainError("invalid_transition", f"状态 {self.state} 不接受事件 {etype}",
                              state=self.state, allowed=allowed)
        event = Event(len(self.events) + 1, etype, actor, iso(occurred_at))
        return Unit(self.id, self.title, self.disciplines, dst, self.version + 1,
                    self.events + (event,), self.snapshot)

    def freeze(self, actor, occurred_at):
        """冻结审核通过的快照；重复冻结幂等返回既有快照。"""
        if self.snapshot is not None:
            return self
        if self.state != "approved":
            raise DomainError("invalid_state", f"仅审核通过(approved)的知识单元可冻结，当前状态: {self.state}",
                              state=self.state, required="approved")
        approved_at = next((e.occurred_at for e in reversed(self.events) if e.type == "approve"), None)
        content = {"id": self.id, "title": self.title, "disciplines": list(self.disciplines),
                   "version": self.version, "approved_at": approved_at}
        digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        snapshot = {**content, "frozen_at": iso(occurred_at), "frozen_by": actor, "content_hash": digest}
        event = Event(len(self.events) + 1, "freeze", actor, iso(occurred_at))
        return Unit(self.id, self.title, self.disciplines, self.state, self.version + 1,
                    self.events + (event,), snapshot)


class Workflow:
    """内存态工作流；冻结快照可写入 SQLite 仓储。"""

    def __init__(self, store=None):
        self.rows, self.keys, self.store = {}, {}, store

    def create(self, id, title, disciplines, actor, occurred_at, key=None):
        if key is not None and key in self.keys:
            return self.rows[self.keys[key]], False
        if id in self.rows:
            raise DomainError("duplicate", f"知识单元已存在: {id}")
        unit = Unit(id, title, tuple(disciplines),
                    events=(Event(1, "propose", actor, iso(occurred_at)),))
        self.rows[id] = unit
        if key is not None:
            self.keys[key] = id
        return unit, True

    def get(self, id):
        if id not in self.rows:
            raise DomainError("not_found", f"知识单元不存在: {id}")
        return self.rows[id]

    def apply(self, id, etype, actor, occurred_at):
        unit = self.get(id).apply(etype, actor, occurred_at)
        self.rows[id] = unit
        return unit

    def freeze(self, id, actor, occurred_at):
        before = self.get(id)
        unit = before.freeze(actor, occurred_at)
        self.rows[id] = unit
        if self.store is not None and before.snapshot is None:
            self.store.save_snapshot(id, unit.snapshot)
        return unit

    def snapshot(self):
        return [self.rows[k].view() for k in sorted(self.rows)]
