"""版本化业务工作流：知识单元状态机、带时区事件、审核快照冻结。"""
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

from .errors import Reject

TRANSITIONS = {
    "draft": {"reviewing", "cancelled"},
    "reviewing": {"approved", "rejected"},
    "rejected": {"draft"},
    "approved": {"archived"},
}


@dataclass(frozen=True)
class Case:
    """跨学科知识单元：版本随每次流转递增。"""

    id: str
    actor: str
    state: str
    title: str
    disciplines: tuple
    created_at: str
    version: int = 1

    def move(self, state, actor):
        allowed = TRANSITIONS.get(self.state, set())
        if state not in allowed:
            raise Reject(
                "invalid_transition",
                f"知识单元 {self.id} 不能从 {self.state} 流转到 {state}",
                [{"field": "state", "reason": "invalid_transition", "current": self.state,
                  "requested": state, "allowed": sorted(allowed)}],
                status=409,
            )
        return replace(self, actor=actor, state=state, version=self.version + 1)


@dataclass(frozen=True)
class Event:
    """带时区的外部事件，occurred_at 已归一化为 UTC，原始偏移单独保留。"""

    seq: int
    id: str
    case_id: str
    actor: str
    kind: str
    occurred_at: str
    tz_offset_minutes: int
    payload: dict
    recorded_at: str


@dataclass(frozen=True)
class FrozenSnapshot:
    """审核通过后冻结的不可变快照，digest 为内容寻址哈希。"""

    case_id: str
    version: int
    digest: str
    frozen_by: str
    frozen_at: str
    content: dict


def case_dict(case):
    data = asdict(case)
    data["disciplines"] = list(data["disciplines"])
    return data


def parse_moment(raw, field="occurred_at"):
    """解析带时区的 ISO 8601 时间；裸时间（无时区偏移）一律拒绝。"""
    if not isinstance(raw, str) or not raw.strip():
        raise Reject("invalid_input", f"{field} 必须是 ISO 8601 字符串",
                     [{"field": field, "reason": "must_be_iso8601_string"}])
    text = raw.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise Reject("invalid_input", f"{field} 不是合法的 ISO 8601 时间",
                     [{"field": field, "reason": "invalid_datetime", "value": raw}])
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise Reject("invalid_input", f"{field} 必须携带时区偏移（如 +08:00 或 Z）",
                     [{"field": field, "reason": "timezone_required", "value": raw}])
    return moment


def _check_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise Reject("invalid_input", f"{field} 必须是非空字符串",
                     [{"field": field, "reason": "must_be_non_empty_string"}])
    return value.strip()


def _check_disciplines(value):
    if not isinstance(value, (list, tuple)) or not value \
            or any(not isinstance(d, str) or not d.strip() for d in value):
        raise Reject("invalid_input", "disciplines 必须是非空学科名字符串列表",
                     [{"field": "disciplines", "reason": "must_be_list_of_discipline_names"}])
    names = [d.strip() for d in value]
    if len(set(names)) < 2:
        raise Reject("invalid_input", "跨学科知识单元至少需要两个不同学科",
                     [{"field": "disciplines", "reason": "need_at_least_2_distinct_disciplines"}])
    return names


def _unknown_case(case_id, field="id"):
    return Reject("unknown_case", f"知识单元 {case_id} 不存在",
                  [{"field": field, "reason": "unknown_case", "value": case_id}], status=404)


class Workflow:
    """知识单元编排核心；store 提供离线持久化，clock 可注入以保证可重现。"""

    def __init__(self, store=None, clock=None):
        self.rows = {}
        self.keys = {}
        self.event_keys = {}
        self.events_log = []
        self.frozen_map = {}
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._seq = 0

    @classmethod
    def load(cls, store, clock=None):
        """从仓储恢复状态，用于离线重启。"""
        flow = cls(store=store, clock=clock)
        state = store.latest()
        if isinstance(state, dict):
            for row in state.get("cases", []):
                row = dict(row, disciplines=tuple(row["disciplines"]))
                flow.rows[row["id"]] = Case(**row)
            flow.keys = dict(state.get("keys", {}))
        for data in store.events():
            event = Event(**data)
            flow.events_log.append(event)
            flow.event_keys[event.id] = event
            flow._seq = max(flow._seq, event.seq)
        for data in store.frozen():
            snap = FrozenSnapshot(**data)
            flow.frozen_map[(snap.case_id, snap.version)] = snap
        return flow

    def _now(self):
        moment = self.clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()

    def _persist(self):
        if self.store:
            self.store.save({"cases": self.snapshot(), "keys": self.keys})

    def get(self, case_id):
        return self.rows.get(case_id)

    def create(self, id, actor, title, disciplines, key=None):
        if key is not None and key in self.keys:
            return self.rows[self.keys[key]]
        if id in self.rows:
            raise Reject("duplicate_case", f"知识单元 {id} 已存在",
                         [{"field": "id", "reason": "duplicate", "value": id}], status=409)
        row = Case(id=_check_text(id, "id"), actor=_check_text(actor, "actor"), state="draft",
                   title=_check_text(title, "title"), disciplines=tuple(_check_disciplines(disciplines)),
                   created_at=self._now())
        self.rows[row.id] = row
        if key is not None:
            self.keys[key] = row.id
        self._persist()
        return row

    def move(self, id, state, actor):
        case = self.rows.get(id)
        if case is None:
            raise _unknown_case(id)
        self.rows[id] = case.move(_check_text(state, "state"), _check_text(actor, "actor"))
        self._persist()
        return self.rows[id]

    def record_event(self, case_id, actor, kind, occurred_at, payload=None, event_id=None):
        if case_id not in self.rows:
            raise _unknown_case(case_id, field="case_id")
        if event_id is not None and event_id in self.event_keys:
            return self.event_keys[event_id]
        moment = parse_moment(occurred_at)
        if payload is not None and not isinstance(payload, dict):
            raise Reject("invalid_input", "payload 必须是对象",
                         [{"field": "payload", "reason": "must_be_object"}])
        self._seq += 1
        event = Event(seq=self._seq, id=event_id or f"evt-{self._seq}", case_id=case_id,
                      actor=_check_text(actor, "actor"), kind=_check_text(kind, "kind"),
                      occurred_at=moment.astimezone(timezone.utc).isoformat(),
                      tz_offset_minutes=int(moment.utcoffset().total_seconds() // 60),
                      payload=dict(payload or {}), recorded_at=self._now())
        self.events_log.append(event)
        self.event_keys[event.id] = event
        if self.store:
            self.store.save_event(asdict(event))
        return event

    def events(self, case_id=None):
        if case_id is not None and case_id not in self.rows:
            raise _unknown_case(case_id, field="case_id")
        return [asdict(e) for e in self.events_log if case_id is None or e.case_id == case_id]

    def freeze(self, id, actor):
        """冻结审核通过(approved)版本的快照；同版本重复冻结为幂等重放。"""
        case = self.rows.get(id)
        if case is None:
            raise _unknown_case(id)
        if case.state != "approved":
            raise Reject(
                "invalid_state",
                f"只有审核通过(approved)的知识单元才能冻结快照，当前状态为 {case.state}",
                [{"field": "state", "reason": "not_approved",
                  "current": case.state, "required": "approved"}],
                status=409,
            )
        key = (id, case.version)
        if key in self.frozen_map:
            return self.frozen_map[key], False
        content = {"case_id": case.id, "version": case.version, "state": case.state,
                   "title": case.title, "disciplines": list(case.disciplines)}
        digest = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        snap = FrozenSnapshot(case_id=id, version=case.version, digest=digest,
                              frozen_by=_check_text(actor, "actor"), frozen_at=self._now(),
                              content=content)
        self.frozen_map[key] = snap
        if self.store:
            self.store.save_frozen(asdict(snap))
        return snap, True

    def frozen(self, case_id=None):
        if case_id is not None and case_id not in self.rows:
            raise _unknown_case(case_id)
        return [asdict(s) for (cid, _), s in sorted(self.frozen_map.items())
                if case_id is None or cid == case_id]

    def snapshot(self):
        return [case_dict(self.rows[k]) for k in sorted(self.rows)]
