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

    def _seed(self) -> str:
        self.app.handle("POST", "/users", body=json.dumps(
            {"user_id": "stat", "display_name": "统计", "role": "statistician"}).encode())
        self.app.handle("POST", "/users", body=json.dumps(
            {"user_id": "auditor", "display_name": "审计", "role": "auditor"}).encode())
        response = self.app.handle(
            "POST", "/workers",
            headers={"X-Actor-Id": "stat"},
            body=json.dumps({"worker_id": "w1", "display_name": "复核进程"}).encode(),
        )
        self.assertEqual(response.status, 201)
        return response.body["credential"]

    def test_worker_routes_require_permission_and_credentials(self) -> None:
        self.app.handle("POST", "/users", body=json.dumps(
            {"user_id": "op", "display_name": "操作员", "role": "operator"}).encode())
        denied = self.app.handle(
            "POST", "/workers",
            headers={"X-Actor-Id": "op"},
            body=json.dumps({"worker_id": "w1", "display_name": "x"}).encode(),
        )
        self.assertEqual(denied.status, 403)
        credential = self._seed()
        claim = self.app.handle(
            "POST", "/jobs/claim",
            headers={"X-Actor-Id": "stat"},
            body=json.dumps({"worker_id": "w1", "credential": "wrong"}).encode(),
        )
        self.assertEqual(claim.status, 403)
        self.assertIsNotNone(credential)

    def test_job_routes_and_lease_audit(self) -> None:
        credential = self._seed()
        empty = self.app.handle(
            "POST", "/jobs/claim",
            headers={"X-Actor-Id": "stat"},
            body=json.dumps({"worker_id": "w1", "credential": credential}).encode(),
        )
        self.assertEqual(empty.status, 200)
        self.assertIsNone(empty.body["job"])
        events = self.app.handle("GET", "/jobs/1/leases", headers={"X-Actor-Id": "auditor"})
        self.assertEqual(events.status, 200)
        self.assertEqual(events.body["events"], [])
        forbidden = self.app.handle("GET", "/jobs/1/leases", headers={"X-Actor-Id": "stat"})
        self.assertEqual(forbidden.status, 403)
        revoke = self.app.handle(
            "POST", "/workers/w1/revoke",
            headers={"X-Actor-Id": "auditor"},
            body=json.dumps({"reason": "回收"}).encode(),
        )
        self.assertEqual(revoke.status, 200)
        after = self.app.handle(
            "POST", "/jobs/claim",
            headers={"X-Actor-Id": "stat"},
            body=json.dumps({"worker_id": "w1", "credential": credential}).encode(),
        )
        self.assertEqual(after.status, 403)


if __name__ == "__main__":
    unittest.main()
