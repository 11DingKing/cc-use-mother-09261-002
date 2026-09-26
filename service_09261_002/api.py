"""JSON API 适配器：输入校验与可解释拒绝。"""
from .workflow import DomainError, parse_event_time


def _need(body, field, details, reason="缺少必填字段"):
    value = body.get(field)
    if value is None or value == "" or value == []:
        details.append({"field": field, "reason": reason})
        return None
    return value


def _event_time(body, details):
    raw = body.get("occurred_at")
    if raw is None:
        details.append({"field": "occurred_at", "reason": "缺少必填字段"})
        return None
    try:
        return parse_event_time(raw)
    except DomainError as exc:
        details.append({"field": "occurred_at", "reason": exc.message})
        return None


def _reject(details):
    return 422, {"error": "invalid_input",
                 "message": "输入不完整或无法解析，请按 details 修正后重试",
                 "details": details}


def _create(flow, body):
    details = []
    uid = _need(body, "id", details)
    title = _need(body, "title", details)
    disciplines = _need(body, "disciplines", details, "缺少必填字段（至少一个学科）")
    actor = _need(body, "actor", details)
    moment = _event_time(body, details)
    if disciplines is not None and not (isinstance(disciplines, list)
                                        and all(isinstance(d, str) and d for d in disciplines)):
        details.append({"field": "disciplines", "reason": "必须是非空字符串数组"})
    if details:
        return _reject(details)
    unit, created = flow.create(uid, title, disciplines, actor, moment, body.get("idempotency_key"))
    return (201 if created else 200), unit.view()


def _event(flow, uid, body):
    flow.get(uid)
    details = []
    etype = _need(body, "type", details)
    actor = _need(body, "actor", details)
    moment = _event_time(body, details)
    if details:
        return _reject(details)
    return 200, flow.apply(uid, etype, actor, moment).view()


def _freeze(flow, uid, body):
    flow.get(uid)
    details = []
    actor = _need(body, "actor", details)
    moment = _event_time(body, details)
    if details:
        return _reject(details)
    return 200, flow.freeze(uid, actor, moment).snapshot


def dispatch(flow, method, path, body=None):
    body = body or {}
    parts = [p for p in path.split("/") if p]
    try:
        if parts == ["units"]:
            if method == "POST":
                return _create(flow, body)
            if method == "GET":
                return 200, {"units": flow.snapshot()}
        if len(parts) == 2 and parts[0] == "units" and method == "GET":
            return 200, flow.get(parts[1]).view()
        if len(parts) == 3 and parts[0] == "units" and method == "POST":
            if parts[2] == "events":
                return _event(flow, parts[1], body)
            if parts[2] == "freeze":
                return _freeze(flow, parts[1], body)
        return 404, {"error": "not_found", "message": f"路由不存在: {method} {path}"}
    except DomainError as exc:
        return (404 if exc.code == "not_found" else 409), exc.payload()
