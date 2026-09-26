"""接口测试：经 dispatch 边界覆盖时区事件、快照冻结与可解释拒绝。"""
import os
import tempfile
import unittest
from datetime import datetime, timezone

from service_09261_002 import SQLiteStore, Workflow
from service_09261_002.api import dispatch

FIXED_CLOCK = lambda: datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def make_flow():
    return Workflow(clock=FIXED_CLOCK)


def create_case(flow, case_id="c1", key=None):
    body = {"id": case_id, "actor": "editor", "title": "气候变化与唐诗",
            "disciplines": ["气象学", "古典文学"]}
    if key:
        body["idempotency_key"] = key
    status, data = dispatch(flow, "POST", "/cases", body)
    assert status == 201, data
    return data


def approve(flow, case_id="c1"):
    dispatch(flow, "POST", f"/cases/{case_id}/move", {"state": "reviewing", "actor": "editor"})
    dispatch(flow, "POST", f"/cases/{case_id}/move", {"state": "approved", "actor": "reviewer"})


class TestFlow(unittest.TestCase):
    def test_flow(self):
        f = Workflow()
        self.assertEqual(f.create("c1", "a", "t", ["数学", "物理"], "k").state, "draft")
        self.assertEqual(f.create("c1", "a", "t", ["数学", "物理"], "k").version, 1)
        self.assertEqual(f.move("c1", "reviewing", "b").state, "reviewing")


class TestCases(unittest.TestCase):
    def test_create_get_and_list(self):
        flow = make_flow()
        body = create_case(flow)
        self.assertEqual(body["state"], "draft")
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["disciplines"], ["气象学", "古典文学"])
        self.assertEqual(body["created_at"], "2026-09-26T12:00:00+00:00")
        status, fetched = dispatch(flow, "GET", "/cases/c1")
        self.assertEqual((status, fetched["title"]), (200, "气候变化与唐诗"))
        status, listing = dispatch(flow, "GET", "/cases")
        self.assertEqual((status, [row["id"] for row in listing]), (200, ["c1"]))

    def test_create_with_idempotency_key_replays(self):
        flow = make_flow()
        first = create_case(flow, key="k-1")
        second = create_case(flow, key="k-1")
        self.assertEqual(first, second)
        _, listing = dispatch(flow, "GET", "/cases")
        self.assertEqual(len(listing), 1)

    def test_get_unknown_case(self):
        status, body = dispatch(make_flow(), "GET", "/cases/ghost")
        self.assertEqual((status, body["error"]), (404, "unknown_case"))


class TestEvents(unittest.TestCase):
    """带时区事件的接收与拒绝路径。"""

    def setUp(self):
        self.flow = make_flow()
        create_case(self.flow)

    def post_event(self, **overrides):
        body = {"case_id": "c1", "actor": "editor", "kind": "unit.annotated",
                "occurred_at": "2026-09-26T20:30:00+08:00", "payload": {"note": "补充物候例证"}}
        body.update(overrides)
        return dispatch(self.flow, "POST", "/events", body)

    def test_event_with_offset_accepted_and_normalized_to_utc(self):
        status, body = self.post_event()
        self.assertEqual(status, 201)
        self.assertEqual(body["occurred_at"], "2026-09-26T12:30:00+00:00")
        self.assertEqual(body["tz_offset_minutes"], 480)
        self.assertEqual(body["seq"], 1)
        self.assertEqual(body["recorded_at"], "2026-09-26T12:00:00+00:00")

    def test_event_with_zulu_and_negative_offset(self):
        status, zulu = self.post_event(occurred_at="2026-09-26T12:30:00Z")
        self.assertEqual((status, zulu["occurred_at"]), (201, "2026-09-26T12:30:00+00:00"))
        status, west = self.post_event(occurred_at="2026-09-26T07:30:00-05:00")
        self.assertEqual((status, west["occurred_at"]), (201, "2026-09-26T12:30:00+00:00"))
        self.assertEqual(west["tz_offset_minutes"], -300)

    def test_naive_datetime_rejected_with_explanation(self):
        status, body = self.post_event(occurred_at="2026-09-26 12:30:00")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "invalid_input")
        self.assertIn("时区", body["message"])
        self.assertEqual(body["details"][0]["reason"], "timezone_required")

    def test_malformed_datetime_rejected(self):
        status, body = self.post_event(occurred_at="not-a-time")
        self.assertEqual((status, body["details"][0]["reason"]), (422, "invalid_datetime"))

    def test_missing_fields_reported_together(self):
        status, body = dispatch(self.flow, "POST", "/events", {"case_id": "c1"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "incomplete_input")
        self.assertEqual({d["field"] for d in body["details"]}, {"actor", "kind", "occurred_at"})

    def test_event_for_unknown_case_rejected(self):
        status, body = self.post_event(case_id="ghost")
        self.assertEqual((status, body["error"]), (404, "unknown_case"))

    def test_event_id_replay_is_idempotent(self):
        _, first = self.post_event(id="evt-9")
        _, second = self.post_event(id="evt-9")
        self.assertEqual(first, second)
        _, events = dispatch(self.flow, "GET", "/cases/c1/events")
        self.assertEqual([e["seq"] for e in events], [1])

    def test_list_events(self):
        self.post_event()
        status, events = dispatch(self.flow, "GET", "/events")
        self.assertEqual((status, len(events)), (200, 1))
        status, events = dispatch(self.flow, "GET", "/cases/c1/events")
        self.assertEqual((status, events[0]["kind"]), (200, "unit.annotated"))


class TestFreeze(unittest.TestCase):
    """审核快照冻结路径。"""

    def setUp(self):
        self.flow = make_flow()
        create_case(self.flow)

    def freeze(self, case_id="c1", actor="reviewer"):
        return dispatch(self.flow, "POST", f"/cases/{case_id}/freeze", {"actor": actor})

    def test_freeze_requires_approved_state(self):
        status, body = self.freeze()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "invalid_state")
        self.assertEqual(body["details"][0]["current"], "draft")
        self.assertEqual(body["details"][0]["required"], "approved")

    def test_freeze_approved_snapshot(self):
        approve(self.flow)
        status, snap = self.freeze()
        self.assertEqual(status, 201)
        self.assertEqual(snap["version"], 3)
        self.assertEqual(snap["frozen_by"], "reviewer")
        self.assertEqual(snap["frozen_at"], "2026-09-26T12:00:00+00:00")
        self.assertEqual(len(snap["digest"]), 64)
        self.assertEqual(snap["content"]["state"], "approved")
        self.assertEqual(snap["content"]["disciplines"], ["气象学", "古典文学"])

    def test_refreeze_same_version_is_replay(self):
        approve(self.flow)
        _, first = self.freeze()
        status, second = self.freeze()
        self.assertEqual(status, 200)
        self.assertEqual(first, second)
        _, snaps = dispatch(self.flow, "GET", "/cases/c1/snapshots")
        self.assertEqual(len(snaps), 1)

    def test_frozen_snapshot_is_immutable_after_case_moves(self):
        approve(self.flow)
        _, snap = self.freeze()
        dispatch(self.flow, "POST", "/cases/c1/move", {"state": "archived", "actor": "editor"})
        _, snaps = dispatch(self.flow, "GET", "/cases/c1/snapshots")
        self.assertEqual(snaps[0]["content"]["state"], "approved")
        self.assertEqual(snaps[0]["version"], 3)
        self.assertEqual(snaps[0]["digest"], snap["digest"])
        status, body = self.freeze()
        self.assertEqual((status, body["details"][0]["current"]), (409, "archived"))

    def test_freeze_unknown_case(self):
        status, body = self.freeze(case_id="ghost")
        self.assertEqual((status, body["error"]), (404, "unknown_case"))

    def test_freeze_missing_actor(self):
        approve(self.flow)
        status, body = dispatch(self.flow, "POST", "/cases/c1/freeze", {})
        self.assertEqual((status, body["error"]), (422, "incomplete_input"))
        self.assertEqual([d["field"] for d in body["details"]], ["actor"])


class TestRejections(unittest.TestCase):
    """输入不完整/非法时的可解释拒绝。"""

    def test_create_missing_fields_listed(self):
        status, body = dispatch(make_flow(), "POST", "/cases", {"actor": "editor"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "incomplete_input")
        self.assertEqual({d["field"] for d in body["details"]}, {"id", "title", "disciplines"})

    def test_create_requires_two_distinct_disciplines(self):
        status, body = dispatch(make_flow(), "POST", "/cases",
                                {"id": "c1", "actor": "a", "title": "t", "disciplines": ["数学"]})
        self.assertEqual((status, body["error"]), (422, "invalid_input"))
        self.assertEqual(body["details"][0]["reason"], "need_at_least_2_distinct_disciplines")

    def test_create_disciplines_must_be_list(self):
        status, body = dispatch(make_flow(), "POST", "/cases",
                                {"id": "c1", "actor": "a", "title": "t", "disciplines": "数学"})
        self.assertEqual((status, body["details"][0]["reason"]),
                         (422, "must_be_list_of_discipline_names"))

    def test_duplicate_case_rejected(self):
        flow = make_flow()
        create_case(flow)
        status, body = dispatch(flow, "POST", "/cases",
                                {"id": "c1", "actor": "a", "title": "t",
                                 "disciplines": ["数学", "物理"]})
        self.assertEqual((status, body["error"]), (409, "duplicate_case"))

    def test_move_missing_state(self):
        flow = make_flow()
        create_case(flow)
        status, body = dispatch(flow, "POST", "/cases/c1/move", {"actor": "a"})
        self.assertEqual((status, body["error"]), (422, "incomplete_input"))
        self.assertEqual([d["field"] for d in body["details"]], ["state"])

    def test_invalid_transition_explains_allowed_targets(self):
        flow = make_flow()
        create_case(flow)
        status, body = dispatch(flow, "POST", "/cases/c1/move",
                                {"state": "approved", "actor": "a"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "invalid_transition")
        detail = body["details"][0]
        self.assertEqual(detail["current"], "draft")
        self.assertEqual(detail["requested"], "approved")
        self.assertEqual(detail["allowed"], ["cancelled", "reviewing"])

    def test_move_unknown_case(self):
        status, body = dispatch(make_flow(), "POST", "/cases/ghost/move",
                                {"state": "reviewing", "actor": "a"})
        self.assertEqual((status, body["error"]), (404, "unknown_case"))

    def test_unknown_route(self):
        status, body = dispatch(make_flow(), "DELETE", "/cases")
        self.assertEqual((status, body["error"]), (404, "not_found"))


class TestPersistence(unittest.TestCase):
    """离线 SQLite 持久化：重启后状态可恢复。"""

    def test_state_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.db")
            flow = Workflow(store=SQLiteStore(path), clock=FIXED_CLOCK)
            create_case(flow)
            approve(flow)
            dispatch(flow, "POST", "/events",
                     {"case_id": "c1", "actor": "editor", "kind": "unit.annotated",
                      "occurred_at": "2026-09-26T20:30:00+08:00"})
            dispatch(flow, "POST", "/cases/c1/freeze", {"actor": "reviewer"})

            restored = Workflow.load(SQLiteStore(path), clock=FIXED_CLOCK)
            _, cases = dispatch(restored, "GET", "/cases")
            self.assertEqual(cases[0]["state"], "approved")
            _, events = dispatch(restored, "GET", "/cases/c1/events")
            self.assertEqual([e["occurred_at"] for e in events], ["2026-09-26T12:30:00+00:00"])
            _, snaps = dispatch(restored, "GET", "/cases/c1/snapshots")
            self.assertEqual([s["version"] for s in snaps], [3])
            status, again = dispatch(restored, "POST", "/cases/c1/freeze", {"actor": "reviewer"})
            self.assertEqual((status, again["digest"]), (200, snaps[0]["digest"]))


if __name__ == "__main__":
    unittest.main()
