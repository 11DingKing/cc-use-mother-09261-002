"""JSON API 适配器：路由分发、必填校验、可解释拒绝。"""
from dataclasses import asdict

from .errors import Reject
from .workflow import case_dict


def _require(body, fields):
    """检查必填字段，返回缺失明细（空串/None 视为缺失）。"""
    details = []
    for name in fields:
        value = body.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            details.append({"field": name, "reason": "missing"})
    return details


def _incomplete(details):
    return 422, {"error": "incomplete_input", "message": "请求缺少必填字段", "details": details}


def _unknown_case(case_id):
    return 404, {"error": "unknown_case", "message": f"知识单元 {case_id} 不存在",
                 "details": [{"field": "id", "reason": "unknown_case", "value": case_id}]}


def dispatch(flow, method, path, body=None):
    """同步 JSON 边界：返回 (status, body)，业务拒绝统一翻译为可解释响应体。"""
    body = body if isinstance(body, dict) else {}
    parts = [p for p in path.split("/") if p]
    try:
        if method == "POST" and parts == ["cases"]:
            details = _require(body, ["id", "actor", "title", "disciplines"])
            if details:
                return _incomplete(details)
            case = flow.create(body["id"], body["actor"], body["title"],
                               body["disciplines"], body.get("idempotency_key"))
            return 201, case_dict(case)
        if method == "POST" and parts == ["events"]:
            details = _require(body, ["case_id", "actor", "kind", "occurred_at"])
            if details:
                return _incomplete(details)
            event = flow.record_event(body["case_id"], body["actor"], body["kind"],
                                      body["occurred_at"], body.get("payload"), body.get("id"))
            return 201, asdict(event)
        if method == "GET" and parts == ["cases"]:
            return 200, flow.snapshot()
        if method == "GET" and parts == ["events"]:
            return 200, flow.events()
        if len(parts) >= 2 and parts[0] == "cases":
            case_id = parts[1]
            if method == "GET" and len(parts) == 2:
                case = flow.get(case_id)
                return (200, case_dict(case)) if case else _unknown_case(case_id)
            if method == "POST" and len(parts) == 3 and parts[2] == "move":
                details = _require(body, ["state", "actor"])
                if details:
                    return _incomplete(details)
                return 200, case_dict(flow.move(case_id, body["state"], body["actor"]))
            if method == "POST" and len(parts) == 3 and parts[2] == "freeze":
                details = _require(body, ["actor"])
                if details:
                    return _incomplete(details)
                snap, created = flow.freeze(case_id, body["actor"])
                return (201 if created else 200), asdict(snap)
            if method == "GET" and len(parts) == 3 and parts[2] == "events":
                return 200, flow.events(case_id)
            if method == "GET" and len(parts) == 3 and parts[2] == "snapshots":
                return 200, flow.frozen(case_id)
    except Reject as reject:
        return reject.status, reject.body()
    return 404, {"error": "not_found"}
