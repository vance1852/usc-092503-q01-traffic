from __future__ import annotations

import json
import sqlite3
import unittest

from evidence_review.api import JsonApplication
from evidence_review.service import EvidenceReviewService


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(EvidenceReviewService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        payload = json.dumps({"user_id": "u1", "display_name": "操作员", "role": "operator"}).encode()
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")

    def _make_user(self, user_id: str, role: str) -> None:
        payload = json.dumps({"user_id": user_id, "display_name": user_id, "role": role}).encode()
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)

    def test_worker_and_claim_routes_require_identity(self) -> None:
        self._make_user("op", "operator")
        self._make_user("stat", "statistician")
        self._make_user("aud", "auditor")
        payload = json.dumps({"worker_id": "w1", "display_name": "复核进程", "owner_user_id": "stat"}).encode()
        response = self.app.handle("POST", "/workers", body=payload)
        self.assertEqual(response.status, 422)
        response = self.app.handle("POST", "/workers", headers={"X-Actor-Id": "op"}, body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["status"], "active")
        claim = json.dumps({"worker_id": "w1", "lease_seconds": 30}).encode()
        response = self.app.handle("POST", "/jobs/claim", headers={"X-Actor-Id": "op"}, body=claim)
        self.assertEqual(response.status, 403)
        response = self.app.handle("POST", "/jobs/claim", headers={"X-Actor-Id": "stat"}, body=claim)
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.body["job"])
        response = self.app.handle("POST", "/jobs/claim", body=claim)
        self.assertEqual(response.status, 422)
        response = self.app.handle("GET", "/workers/w1/events", headers={"X-Actor-Id": "op"})
        self.assertEqual(response.status, 403)
        response = self.app.handle("GET", "/workers/w1/events", headers={"X-Actor-Id": "aud"})
        self.assertEqual(response.status, 200)
        types = [event["event_type"] for event in response.body["events"]]
        self.assertIn("worker.registered", types)
        self.assertIn("job.claim_rejected", types)

    def test_revoke_route(self) -> None:
        self._make_user("op", "operator")
        self._make_user("stat", "statistician")
        payload = json.dumps({"worker_id": "w1", "display_name": "复核进程", "owner_user_id": "stat"}).encode()
        self.app.handle("POST", "/workers", headers={"X-Actor-Id": "op"}, body=payload)
        response = self.app.handle(
            "POST", "/workers/w1/revoke", headers={"X-Actor-Id": "op"},
            body=json.dumps({"reason": "进程失联"}).encode(),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "revoked")
        claim = json.dumps({"worker_id": "w1"}).encode()
        response = self.app.handle("POST", "/jobs/claim", headers={"X-Actor-Id": "stat"}, body=claim)
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
