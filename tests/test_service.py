from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from evidence_review.clock import FrozenClock
from evidence_review.errors import Conflict, Forbidden, InvalidState, NotFound
from evidence_review.jsonio import load_json
from evidence_review.service import EvidenceReviewService
from evidence_review.storage import connect


ROOT = Path(__file__).resolve().parents[1]


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = EvidenceReviewService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("stat-2", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.evidence_protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_device("operator", "device-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "device-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", self.evidence_protocol)
        self.service.create_batch("operator", "batch-a", "demo-evidence-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.credential_a = self.service.register_worker("stat", "worker-a", "甲进程")["credential"]
        self.credential_b = self.service.register_worker("stat", "worker-b", "乙进程")["credential"]

    def tearDown(self) -> None:
        self.connection.close()

    def _sealed_job(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)

    def test_complete_workflow(self) -> None:
        imported = self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(imported["inserted"], 6)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("stat", "worker-a", self.credential_a, 30)
        analysis = self.service.complete_job(
            "stat", "worker-a", self.credential_a, job["job_id"], job["lease_fingerprint"]
        )
        self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "满足规则")
        report = self.service.report("auditor", "batch-a")
        self.assertEqual(report["batch"]["state"], "decided")
        self.assertEqual(report["analysis"]["result"]["conclusion"], "pass")

    def test_idempotent_replay_and_conflict(self) -> None:
        first = self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        second = self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(first, second)
        changed = [dict(item) for item in self.rows]
        changed[0] = dict(changed[0])
        changed[0]["indicators"] = dict(changed[0]["indicators"])
        changed[0]["indicators"]["completion_seconds"] = "99"
        with self.assertRaises(Conflict):
            self.service.import_evidence_items("operator", "batch-a", "key-1", changed)
        count = self.connection.execute("SELECT count(*) FROM evidence_items").fetchone()[0]
        self.assertEqual(count, 6)

    def test_import_rolls_back_when_one_source_row_duplicates(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows[:1])
        with self.assertRaises(Conflict):
            self.service.import_evidence_items("operator", "batch-a", "key-2", self.rows[:2])
        count = self.connection.execute("SELECT count(*) FROM evidence_items").fetchone()[0]
        self.assertEqual(count, 1)

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.seal_batch("operator", "batch-a", 2)
        with self.assertRaises(Forbidden):
            self.service.report("operator", "batch-a")

    def test_exclusion_review_and_revoke_leave_history(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        evidence_item_id = self.connection.execute(
            "SELECT evidence_item_id FROM evidence_items ORDER BY evidence_item_id LIMIT 1"
        ).fetchone()[0]
        requested = self.service.request_exclusion("operator", evidence_item_id, "现场记录失效")
        reviewed = self.service.review_exclusion("stat", requested["exclusion_id"], True, "证据充分")
        self.assertEqual(reviewed["status"], "approved")
        revoked = self.service.revoke_exclusion("operator", requested["exclusion_id"], "已找回原始记录")
        self.assertEqual(revoked["status"], "revoked")
        events = self.connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='evidence_item' AND entity_id=? ORDER BY event_id",
            (str(evidence_item_id),),
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["exclusion.requested", "exclusion.revoked"])

    def test_failed_job_returns_to_queue_after_delay(self) -> None:
        self._sealed_job()
        job = self.service.claim_job("stat", "worker-a", self.credential_a, 10)
        failed = self.service.fail_job(
            "stat", "worker-a", self.credential_a, job["job_id"], job["lease_fingerprint"],
            "临时计算失败", retry_seconds=5,
        )
        self.assertEqual(failed["state"], "queued")
        self.assertIsNone(self.service.claim_job("stat", "worker-b", self.credential_b, 10))
        self.clock.advance(seconds=5)
        retried = self.service.claim_job("stat", "worker-b", self.credential_b, 10)
        self.assertEqual(retried["job_id"], job["job_id"])
        self.assertEqual(retried["attempts"], 2)

    def test_lease_can_be_reclaimed_after_expiry(self) -> None:
        self._sealed_job()
        first = self.service.claim_job("stat", "worker-a", self.credential_a, 10)
        self.clock.advance(seconds=11)
        second = self.service.claim_job("stat", "worker-b", self.credential_b, 10)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["lease_owner"], "worker-b")
        self.assertEqual(second["attempts"], 2)
        with self.assertRaises(InvalidState):
            self.service.complete_job(
                "stat", "worker-a", self.credential_a, first["job_id"], first["lease_fingerprint"]
            )

    def test_claim_requires_registered_worker_and_valid_credential(self) -> None:
        self._sealed_job()
        with self.assertRaises(NotFound):
            self.service.claim_job("stat", "stranger", "whatever", 10)
        with self.assertRaises(Forbidden):
            self.service.claim_job("stat", "worker-a", "wrong-credential", 10)
        with self.assertRaises(Forbidden):
            self.service.claim_job("operator", "worker-a", self.credential_a, 10)
        self.assertIsNone(
            self.connection.execute("SELECT 1 FROM job_lease_events").fetchone()
        )

    def test_lease_owner_is_bound_to_worker_and_person(self) -> None:
        self._sealed_job()
        job = self.service.claim_job("stat", "worker-a", self.credential_a, 30)
        row = self.connection.execute(
            "SELECT lease_owner,lease_actor,lease_fingerprint,lease_expires_at FROM analysis_jobs"
        ).fetchone()
        self.assertEqual(row["lease_owner"], "worker-a")
        self.assertEqual(row["lease_actor"], "stat")
        self.assertTrue(row["lease_fingerprint"])
        self.assertTrue(row["lease_expires_at"])
        self.assertEqual(job["lease_owner"], "worker-a")

    def test_duplicate_claim_is_idempotent_without_extra_attempt(self) -> None:
        self._sealed_job()
        first = self.service.claim_job("stat", "worker-a", self.credential_a, 10)
        again = self.service.claim_job("stat", "worker-a", self.credential_a, 10)
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertEqual(again["attempts"], 1)
        self.assertEqual(again["lease_fingerprint"], first["lease_fingerprint"])
        events = self.service.job_lease_events("auditor", first["job_id"])
        self.assertEqual([event["action"] for event in events], ["acquired", "rejected"])
        self.assertEqual(events[1]["reason"], "duplicate_claim_active_lease")

    def test_other_worker_cannot_take_or_complete_active_lease(self) -> None:
        self._sealed_job()
        job = self.service.claim_job("stat", "worker-a", self.credential_a, 10)
        self.assertIsNone(self.service.claim_job("stat", "worker-b", self.credential_b, 10))
        with self.assertRaises(InvalidState):
            self.service.complete_job(
                "stat", "worker-b", self.credential_b, job["job_id"], job["lease_fingerprint"]
            )
        with self.assertRaises(InvalidState):
            self.service.fail_job(
                "stat", "worker-b", self.credential_b, job["job_id"], job["lease_fingerprint"], "抢任务"
            )

    def test_renew_extends_lease_only_for_owner(self) -> None:
        self._sealed_job()
        job = self.service.claim_job("stat", "worker-a", self.credential_a, 10)
        old_expiry = job["lease_expires_at"]
        renewed = self.service.renew_job("stat", "worker-a", self.credential_a, job["job_id"], 30)
        self.assertGreater(renewed["lease_expires_at"], old_expiry)
        with self.assertRaises(InvalidState):
            self.service.renew_job("stat", "worker-b", self.credential_b, job["job_id"], 30)
        self.clock.advance(seconds=31)
        with self.assertRaises(InvalidState):
            self.service.renew_job("stat", "worker-a", self.credential_a, job["job_id"], 30)

    def test_takeover_after_expiry_rejects_late_submission_from_old_owner(self) -> None:
        self._sealed_job()
        first = self.service.claim_job("stat", "worker-a", self.credential_a, 10)
        self.clock.advance(seconds=11)
        second = self.service.claim_job("stat-2", "worker-b", self.credential_b, 10)
        self.assertNotEqual(first["lease_fingerprint"], second["lease_fingerprint"])
        # 旧持有者迟到完成：即使带着旧指纹也被拒绝。
        with self.assertRaises(InvalidState):
            self.service.complete_job(
                "stat", "worker-a", self.credential_a, first["job_id"], first["lease_fingerprint"]
            )
        # 新持有者正常完成。
        analysis = self.service.complete_job(
            "stat-2", "worker-b", self.credential_b, second["job_id"], second["lease_fingerprint"]
        )
        self.assertTrue(analysis["analysis_id"])

    def test_revoked_worker_and_deactivated_owner_cannot_operate(self) -> None:
        self._sealed_job()
        job = self.service.claim_job("stat", "worker-a", self.credential_a, 30)
        self.service.revoke_worker("auditor", "worker-a", "进程回收")
        with self.assertRaises(Forbidden):
            self.service.renew_job("stat", "worker-a", self.credential_a, job["job_id"], 30)
        with self.assertRaises(Forbidden):
            self.service.complete_job(
                "stat", "worker-a", self.credential_a, job["job_id"], job["lease_fingerprint"]
            )
        credential_c = self.service.register_worker("stat-2", "worker-c", "丙进程")["credential"]
        self.service.deactivate_user("auditor", "stat-2", "账号停用")
        with self.assertRaises(Forbidden):
            self.service.claim_job("stat-2", "worker-c", credential_c, 30)

    def test_revoke_during_long_analysis_blocks_final_commit(self) -> None:
        self._sealed_job()
        job = self.service.claim_job("stat", "worker-a", self.credential_a, 30)
        # 模拟长任务执行期间被撤销：认证已通过，写事务内守卫必须兜底。
        self.service._authenticate_worker = lambda worker_id, credential: None  # type: ignore[method-assign]
        self.service.revoke_worker("auditor", "worker-a", "进程回收")
        with self.assertRaises(Forbidden):
            self.service.complete_job(
                "stat", "worker-a", self.credential_a, job["job_id"], job["lease_fingerprint"]
            )
        events = self.service.job_lease_events("auditor", job["job_id"])
        self.assertEqual(events[-1]["action"], "rejected")
        self.assertEqual(events[-1]["reason"], "worker_revoked")
        # 任务仍归原持有者，过期后仍可被合法进程接管。
        self.clock.advance(seconds=31)
        retried = self.service.claim_job("stat", "worker-b", self.credential_b, 10)
        self.assertEqual(retried["job_id"], job["job_id"])
        self.assertEqual(retried["attempts"], 2)

    def test_attempts_ownership_and_events_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "persist.sqlite3"
            first_connection = connect(database)
            first_service = EvidenceReviewService(first_connection, self.clock)
            for user_id, role in (
                ("operator", "operator"), ("stat", "statistician"), ("auditor", "auditor"),
            ):
                first_service.create_user(user_id, user_id, role)
            first_service.register_device("operator", "device-a", "A 型", "厂商")
            first_service.register_build("operator", "build-a", "device-a", "1.0", "b" * 64)
            first_service.publish_evidence_protocol("stat", self.evidence_protocol)
            first_service.create_batch("operator", "batch-a", "demo-evidence-v1", 1, "build-a")
            first_service.start_batch("operator", "batch-a", 1)
            first_service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
            first_service.seal_batch("stat", "batch-a", 2)
            credential = first_service.register_worker("stat", "worker-a", "甲进程")["credential"]
            claimed = first_service.claim_job("stat", "worker-a", credential, 10)
            first_connection.close()

            second_connection = connect(database)
            second_service = EvidenceReviewService(second_connection, self.clock)
            persisted = second_connection.execute(
                "SELECT attempts,lease_owner,lease_actor FROM analysis_jobs WHERE job_id=?",
                (claimed["job_id"],),
            ).fetchone()
            self.assertEqual(persisted["attempts"], 1)
            self.assertEqual(persisted["lease_owner"], "worker-a")
            self.assertEqual(persisted["lease_actor"], "stat")
            events = second_service.job_lease_events("auditor", claimed["job_id"])
            self.assertEqual(events[0]["action"], "acquired")
            self.assertEqual(events[0]["worker_id"], "worker-a")
            self.assertEqual(events[0]["actor_id"], "stat")
            second_connection.close()

    def test_lease_events_record_every_acquisition_release_and_rejection(self) -> None:
        self._sealed_job()
        job = self.service.claim_job("stat", "worker-a", self.credential_a, 10)
        self.service.renew_job("stat", "worker-a", self.credential_a, job["job_id"], 20)
        self.service.fail_job(
            "stat", "worker-a", self.credential_a, job["job_id"], job["lease_fingerprint"],
            "计算失败", retry_seconds=0,
        )
        retried = self.service.claim_job("stat", "worker-b", self.credential_b, 10)
        self.service.complete_job(
            "stat", "worker-b", self.credential_b, retried["job_id"], retried["lease_fingerprint"]
        )
        events = self.service.job_lease_events("auditor", job["job_id"])
        self.assertEqual(
            [(event["action"], event["worker_id"]) for event in events],
            [("acquired", "worker-a"), ("renewed", "worker-a"), ("failed", "worker-a"),
             ("acquired", "worker-b"), ("completed", "worker-b")],
        )
        with self.assertRaises(Forbidden):
            self.service.job_lease_events("operator", job["job_id"])


if __name__ == "__main__":
    unittest.main()
