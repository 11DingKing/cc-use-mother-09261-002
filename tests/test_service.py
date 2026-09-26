"""接口测试：带时区事件、快照冻结、可解释拒绝。"""
import unittest

from service_09261_002 import SQLiteStore, Workflow, dispatch

BODY = {"id": "u1", "title": "光合作用与能量转换", "disciplines": ["生物", "化学"],
        "actor": "lin", "occurred_at": "2026-09-26T10:00:00+08:00"}


def make_flow():
    flow = Workflow()
    status, _ = dispatch(flow, "POST", "/units", dict(BODY))
    assert status == 201
    return flow


def post_event(flow, uid, etype, **over):
    body = {"type": etype, "actor": "wei", "occurred_at": "2026-09-26T11:00:00+08:00"}
    body.update(over)
    return dispatch(flow, "POST", f"/units/{uid}/events", body)


def approve(flow, uid="u1"):
    assert post_event(flow, uid, "submit_review")[0] == 200
    assert post_event(flow, uid, "approve")[0] == 200


class TestTimezoneEvents(unittest.TestCase):
    def test_offset_time_normalized_to_utc(self):
        status, view = dispatch(Workflow(), "POST", "/units", dict(BODY))
        self.assertEqual(status, 201)
        self.assertEqual(view["events"][0]["occurred_at"], "2026-09-26T02:00:00Z")
        self.assertEqual(view["state"], "draft")

    def test_zulu_and_offset_are_equivalent(self):
        flow = Workflow()
        dispatch(flow, "POST", "/units", dict(BODY))
        other = dict(BODY, id="u2", occurred_at="2026-09-26T02:00:00Z")
        _, view = dispatch(flow, "POST", "/units", other)
        _, first = dispatch(flow, "GET", "/units/u1")
        self.assertEqual(view["events"][0]["occurred_at"], first["events"][0]["occurred_at"])

    def test_naive_time_rejected_with_explanation(self):
        status, err = dispatch(Workflow(), "POST", "/units",
                               dict(BODY, occurred_at="2026-09-26 10:00:00"))
        self.assertEqual(status, 422)
        self.assertEqual(err["error"], "invalid_input")
        self.assertIn("时区", err["details"][0]["reason"])
        self.assertEqual(err["details"][0]["field"], "occurred_at")

    def test_unparseable_time_rejected(self):
        status, err = dispatch(Workflow(), "POST", "/units", dict(BODY, occurred_at="not-a-time"))
        self.assertEqual(status, 422)
        self.assertIn("无法解析", err["details"][0]["reason"])


class TestExplainableRejection(unittest.TestCase):
    def test_missing_fields_listed_together(self):
        status, err = dispatch(Workflow(), "POST", "/units", {"title": "x"})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"], "invalid_input")
        self.assertTrue(err["message"])
        self.assertEqual({d["field"] for d in err["details"]},
                         {"id", "disciplines", "actor", "occurred_at"})
        self.assertTrue(all(d["reason"] for d in err["details"]))

    def test_bad_disciplines_type_explained(self):
        status, err = dispatch(Workflow(), "POST", "/units", dict(BODY, disciplines="生物"))
        self.assertEqual(status, 422)
        self.assertEqual(err["details"][0]["field"], "disciplines")

    def test_event_missing_type_and_actor(self):
        flow = make_flow()
        status, err = dispatch(flow, "POST", "/units/u1/events",
                               {"occurred_at": "2026-09-26T11:00:00Z"})
        self.assertEqual(status, 422)
        self.assertEqual({d["field"] for d in err["details"]}, {"type", "actor"})

    def test_invalid_transition_reports_allowed(self):
        flow = make_flow()
        status, err = post_event(flow, "u1", "approve")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "invalid_transition")
        self.assertEqual(err["state"], "draft")
        self.assertEqual(err["allowed"], ["cancel", "submit_review"])

    def test_unknown_event_type_lists_known(self):
        flow = make_flow()
        status, err = post_event(flow, "u1", "explode")
        self.assertEqual(status, 409)
        self.assertIn("submit_review", err["allowed"])

    def test_unknown_unit_and_route(self):
        flow = Workflow()
        self.assertEqual(dispatch(flow, "GET", "/units/nope")[0], 404)
        self.assertEqual(dispatch(flow, "POST", "/units/nope/events",
                                  {"type": "cancel", "actor": "a",
                                   "occurred_at": "2026-09-26T00:00:00Z"})[0], 404)
        self.assertEqual(dispatch(flow, "GET", "/nope")[0], 404)

    def test_duplicate_id_rejected(self):
        flow = make_flow()
        status, err = dispatch(flow, "POST", "/units", dict(BODY, idempotency_key=None))
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "duplicate")


class TestSnapshotFreeze(unittest.TestCase):
    def test_freeze_requires_approved_state(self):
        flow = make_flow()
        status, err = dispatch(flow, "POST", "/units/u1/freeze",
                               {"actor": "wei", "occurred_at": "2026-09-26T12:00:00Z"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "invalid_state")
        self.assertIn("approved", err["message"])

    def test_review_flow_then_freeze(self):
        flow = make_flow()
        approve(flow)
        status, snap = dispatch(flow, "POST", "/units/u1/freeze",
                                {"actor": "wei", "occurred_at": "2026-09-26T12:00:00+08:00"})
        self.assertEqual(status, 200)
        self.assertEqual(snap["id"], "u1")
        self.assertEqual(snap["approved_at"], "2026-09-26T03:00:00Z")
        self.assertEqual(snap["frozen_at"], "2026-09-26T04:00:00Z")
        self.assertEqual(snap["frozen_by"], "wei")
        self.assertEqual(len(snap["content_hash"]), 64)
        _, view = dispatch(flow, "GET", "/units/u1")
        self.assertTrue(view["frozen"])
        self.assertEqual(view["state"], "approved")

    def test_frozen_unit_rejects_further_events(self):
        flow = make_flow()
        approve(flow)
        dispatch(flow, "POST", "/units/u1/freeze",
                 {"actor": "wei", "occurred_at": "2026-09-26T12:00:00Z"})
        status, err = post_event(flow, "u1", "revise")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "frozen")

    def test_refreeze_is_idempotent(self):
        flow = make_flow()
        approve(flow)
        body = {"actor": "wei", "occurred_at": "2026-09-26T12:00:00Z"}
        _, first = dispatch(flow, "POST", "/units/u1/freeze", body)
        _, again = dispatch(flow, "POST", "/units/u1/freeze",
                            dict(body, actor="someone-else"))
        self.assertEqual(first["content_hash"], again["content_hash"])
        self.assertEqual(again["frozen_by"], "wei")

    def test_reject_and_revise_cycle(self):
        flow = make_flow()
        post_event(flow, "u1", "submit_review")
        _, view = post_event(flow, "u1", "reject")
        self.assertEqual(view["state"], "rejected")
        _, view = post_event(flow, "u1", "revise")
        self.assertEqual(view["state"], "draft")
        approve(flow)
        self.assertEqual(dispatch(flow, "POST", "/units/u1/freeze",
                                  {"actor": "w", "occurred_at": "2026-09-26T12:00:00Z"})[0], 200)

    def test_frozen_snapshot_persisted(self):
        store = SQLiteStore()
        flow = Workflow(store=store)
        dispatch(flow, "POST", "/units", dict(BODY))
        approve(flow)
        _, snap = dispatch(flow, "POST", "/units/u1/freeze",
                           {"actor": "wei", "occurred_at": "2026-09-26T12:00:00Z"})
        self.assertEqual(store.get_snapshot("u1"), snap)
        self.assertEqual(len(store.list_snapshots()), 1)


class TestIdempotencyAndListing(unittest.TestCase):
    def test_idempotency_key_replays_create(self):
        flow = Workflow()
        s1, v1 = dispatch(flow, "POST", "/units", dict(BODY, idempotency_key="k1"))
        s2, v2 = dispatch(flow, "POST", "/units", dict(BODY, idempotency_key="k1"))
        self.assertEqual((s1, s2), (201, 200))
        self.assertEqual(v1, v2)
        _, listing = dispatch(flow, "GET", "/units")
        self.assertEqual(len(listing["units"]), 1)

    def test_listing_sorted_by_id(self):
        flow = Workflow()
        dispatch(flow, "POST", "/units", dict(BODY, id="u2"))
        dispatch(flow, "POST", "/units", dict(BODY, id="u1"))
        _, listing = dispatch(flow, "GET", "/units")
        self.assertEqual([u["id"] for u in listing["units"]], ["u1", "u2"])


if __name__ == "__main__":
    unittest.main()
