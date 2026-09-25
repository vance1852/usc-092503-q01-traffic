"""复核队列租约治理：登记、身份绑定、确定性结果与审计。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evidence_review.clock import FrozenClock, isoformat
from evidence_review.errors import Conflict, Forbidden, InvalidState, NotFound
from evidence_review.jsonio import load_json
from evidence_review.service import EvidenceReviewService
from evidence_review.storage import connect


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


def prepare_sealed_batch(service: EvidenceReviewService) -> None:
    """登记基础资料并把批次推进到已封存、任务已入队。"""

    for user_id, role in (
        ("operator", "operator"),
        ("stat", "statistician"),
        ("approver", "approver"),
        ("auditor", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    evidence_protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
    rows = [
        json.loads(line)
        for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    service.register_device("operator", "device-a", "A 型", "厂商")
    service.register_build("operator", "build-a", "device-a", "1.0", "b" * 64)
    service.publish_evidence_protocol("stat", evidence_protocol)
    service.create_batch("operator", "batch-a", "demo-evidence-v1", 1, "build-a")
    service.start_batch("operator", "batch-a", 1)
    service.import_evidence_items("operator", "batch-a", "key-1", rows)
    service.seal_batch("stat", "batch-a", 2)


class JobLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(NOW)
        self.service = EvidenceReviewService(self.connection, self.clock)
        prepare_sealed_batch(self.service)
        self.service.register_worker("operator", "worker-a", "复核进程 A", "stat")
        self.service.register_worker("operator", "worker-b", "复核进程 B", "stat")

    def tearDown(self) -> None:
        self.connection.close()

    def job_events(self, **filters):
        return self.service.list_job_events("auditor", **filters)

    def event_types(self, **filters) -> list[str]:
        return [event["event_type"] for event in self.job_events(**filters)]

    def test_unregistered_worker_claim_is_rejected_and_audited(self) -> None:
        with self.assertRaises(NotFound):
            self.service.claim_job("stat", "ghost", 30)
        events = self.job_events(worker_id="ghost")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "job.claim_rejected")
        self.assertEqual(events[0]["actor_id"], "stat")
        self.assertIn("未登记", events[0]["payload"]["reason"])

    def test_claim_requires_personnel_permission(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.claim_job("operator", "worker-a", 30)
        events = [e for e in self.job_events(worker_id="worker-a") if e["event_type"] == "job.claim_rejected"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["code"], "forbidden")

    def test_deactivated_user_cannot_operate_queue(self) -> None:
        job = self.service.claim_job("stat", "worker-a", 30)
        self.connection.execute("UPDATE users SET active=0 WHERE user_id='stat'")
        with self.assertRaises(Forbidden):
            self.service.claim_job("stat", "worker-b", 30)
        with self.assertRaises(Forbidden):
            self.service.renew_job("stat", "worker-a", job["job_id"], 30)
        with self.assertRaises(Forbidden):
            self.service.complete_job("stat", "worker-a", job["job_id"])
        with self.assertRaises(Forbidden):
            self.service.fail_job("stat", "worker-a", job["job_id"], "err")
        rejected = [t for t in self.event_types(job_id=job["job_id"]) if t.endswith("_rejected")]
        self.assertEqual(rejected, ["job.renew_rejected", "job.complete_rejected", "job.fail_rejected"])

    def test_claim_binds_traceable_identity(self) -> None:
        job = self.service.claim_job("stat", "worker-a", 30)
        self.assertEqual(job["lease_owner"], "worker-a")
        self.assertEqual(job["lease_actor"], "stat")
        claimed = [e for e in self.job_events(job_id=job["job_id"]) if e["event_type"] == "job.claimed"]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["actor_id"], "stat")
        self.assertEqual(claimed[0]["payload"]["worker_id"], "worker-a")
        self.assertFalse(claimed[0]["payload"]["takeover"])

    def test_duplicate_claim_returns_current_lease(self) -> None:
        first = self.service.claim_job("stat", "worker-a", 30)
        second = self.service.claim_job("stat", "worker-a", 30)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["attempts"], 1)
        self.assertEqual(first["lease_expires_at"], second["lease_expires_at"])
        self.assertEqual(self.event_types(job_id=first["job_id"]), ["job.claimed"])

    def test_renew_extends_lease_and_is_audited(self) -> None:
        job = self.service.claim_job("stat", "worker-a", 30)
        self.clock.advance(seconds=10)
        renewed = self.service.renew_job("stat", "worker-a", job["job_id"], 60)
        self.assertGreater(renewed["lease_expires_at"], job["lease_expires_at"])
        self.assertEqual(renewed["attempts"], 1)
        self.assertEqual(
            self.event_types(job_id=job["job_id"]), ["job.claimed", "job.renewed"]
        )

    def test_renew_by_other_worker_or_after_expiry_is_rejected(self) -> None:
        job = self.service.claim_job("stat", "worker-a", 10)
        with self.assertRaises(InvalidState):
            self.service.renew_job("stat", "worker-b", job["job_id"], 30)
        self.clock.advance(seconds=11)
        with self.assertRaises(InvalidState):
            self.service.renew_job("stat", "worker-a", job["job_id"], 30)
        events = [e for e in self.job_events(job_id=job["job_id"]) if e["event_type"] == "job.renew_rejected"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["payload"]["worker_id"], "worker-b")
        self.assertIn("未由当前工作进程持有", events[0]["payload"]["reason"])
        self.assertIn("过期", events[1]["payload"]["reason"])

    def test_takeover_after_expiry_and_late_submission_are_deterministic(self) -> None:
        first = self.service.claim_job("stat", "worker-a", 10)
        self.clock.advance(seconds=11)
        second = self.service.claim_job("stat", "worker-b", 10)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["attempts"], 2)
        claimed = [e for e in self.job_events(job_id=first["job_id"]) if e["event_type"] == "job.claimed"]
        self.assertEqual(len(claimed), 2)
        self.assertTrue(claimed[1]["payload"]["takeover"])
        self.assertEqual(claimed[1]["payload"]["previous_owner"], "worker-a")
        with self.assertRaises(InvalidState):
            self.service.complete_job("stat", "worker-a", first["job_id"])
        with self.assertRaises(InvalidState):
            self.service.fail_job("stat", "worker-a", first["job_id"], "迟到失败")
        rejected = [t for t in self.event_types(job_id=first["job_id"]) if t.endswith("_rejected")]
        self.assertEqual(rejected, ["job.complete_rejected", "job.fail_rejected"])
        analysis = self.service.complete_job("stat", "worker-b", first["job_id"])
        self.assertIn("analysis_id", analysis)
        self.assertEqual(
            self.event_types(job_id=first["job_id"])[-1], "job.completed"
        )

    def test_expired_lease_cannot_complete_even_without_takeover(self) -> None:
        job = self.service.claim_job("stat", "worker-a", 10)
        self.clock.advance(seconds=11)
        with self.assertRaises(InvalidState):
            self.service.complete_job("stat", "worker-a", job["job_id"])
        with self.assertRaises(InvalidState):
            self.service.fail_job("stat", "worker-a", job["job_id"], "迟到")
        row = self.connection.execute(
            "SELECT state,lease_owner FROM analysis_jobs WHERE job_id=?", (job["job_id"],)
        ).fetchone()
        self.assertEqual(row["state"], "leased")
        self.assertEqual(row["lease_owner"], "worker-a")

    def test_lease_expiring_during_completion_is_rejected_atomically(self) -> None:
        job = self.service.claim_job("stat", "worker-a", 10)
        # 预检时租约仍有效，写入提交前租约已过期：条件更新必须原子拒绝
        stamps = iter([
            isoformat(NOW + timedelta(seconds=5)),
            isoformat(NOW + timedelta(seconds=11)),
        ])
        original = self.service._now
        self.service._now = lambda: next(stamps, isoformat(NOW + timedelta(seconds=12)))
        try:
            with self.assertRaises(InvalidState):
                self.service.complete_job("stat", "worker-a", job["job_id"])
        finally:
            self.service._now = original
        row = self.connection.execute(
            "SELECT state,lease_owner FROM analysis_jobs WHERE job_id=?", (job["job_id"],)
        ).fetchone()
        self.assertEqual(row["state"], "leased")
        self.assertEqual(row["lease_owner"], "worker-a")
        analyses = self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0]
        self.assertEqual(analyses, 0)
        events = [e for e in self.job_events(job_id=job["job_id"]) if e["event_type"] == "job.complete_rejected"]
        self.assertEqual(len(events), 1)
        self.assertIn("失效", events[0]["payload"]["reason"])

    def test_revoked_worker_releases_lease_and_cannot_operate(self) -> None:
        job = self.service.claim_job("stat", "worker-a", 600)
        result = self.service.revoke_worker("operator", "worker-a", "进程失联，回收任务")
        self.assertEqual(result["released_jobs"], [job["job_id"]])
        reclaimed = self.service.claim_job("stat", "worker-b", 30)
        self.assertEqual(reclaimed["job_id"], job["job_id"])
        for call in (
            lambda: self.service.claim_job("stat", "worker-a", 30),
            lambda: self.service.renew_job("stat", "worker-a", job["job_id"], 30),
            lambda: self.service.complete_job("stat", "worker-a", job["job_id"]),
            lambda: self.service.fail_job("stat", "worker-a", job["job_id"], "err"),
        ):
            with self.assertRaises(Forbidden):
                call()
        types = self.event_types(job_id=job["job_id"])
        self.assertIn("job.released_revoked", types)
        worker_types = self.event_types(worker_id="worker-a")
        self.assertIn("job.claim_rejected", worker_types)
        self.assertIn("worker.revoked", worker_types)
        with self.assertRaises(InvalidState):
            self.service.revoke_worker("operator", "worker-a", "重复撤销")

    def test_revoke_requires_reason_and_registered_worker(self) -> None:
        with self.assertRaises(NotFound):
            self.service.revoke_worker("operator", "ghost", "原因")
        with self.assertRaises(Forbidden):
            self.service.register_worker("stat", "worker-c", "越权登记", "stat")
        with self.assertRaises(Conflict):
            self.service.register_worker("operator", "worker-a", "重复登记", "stat")

    def test_job_events_require_auditor_permission(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.list_job_events("operator")
        job = self.service.claim_job("stat", "worker-a", 30)
        by_batch = self.job_events(batch_id="batch-a")
        self.assertEqual([e["event_type"] for e in by_batch], ["job.claimed"])
        self.assertEqual(by_batch[0]["entity_id"], str(job["job_id"]))
        by_worker = self.job_events(worker_id="worker-a")
        self.assertTrue(any(e["event_type"] == "worker.registered" for e in by_worker))
        self.assertTrue(any(e["event_type"] == "job.claimed" for e in by_worker))


class JobLeaseRestartTests(unittest.TestCase):
    """队列重启后仍保留尝试次数、所有权与审计。"""

    def test_restart_preserves_attempts_ownership_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "queue.sqlite3"
            clock = FrozenClock(NOW)
            connection = connect(database)
            service = EvidenceReviewService(connection, clock)
            prepare_sealed_batch(service)
            service.register_worker("operator", "worker-a", "复核进程 A", "stat")
            service.register_worker("operator", "worker-b", "复核进程 B", "stat")
            first = service.claim_job("stat", "worker-a", 10)
            service.fail_job("stat", "worker-a", first["job_id"], "崩溃前失败", retry_seconds=5)
            connection.close()

            clock.advance(seconds=5)
            restarted = connect(database)
            try:
                service2 = EvidenceReviewService(restarted, clock)
                second = service2.claim_job("stat", "worker-b", 10)
                self.assertEqual(second["job_id"], first["job_id"])
                self.assertEqual(second["attempts"], 2)
                self.assertEqual(second["lease_owner"], "worker-b")
                self.assertEqual(second["lease_actor"], "stat")
                events = service2.list_job_events("auditor", job_id=first["job_id"])
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["job.claimed", "job.failed", "job.claimed"],
                )
                self.assertEqual(events[0]["payload"]["worker_id"], "worker-a")
                self.assertEqual(events[1]["payload"]["error"], "崩溃前失败")
                # fail_job 主动释放后任务回到队列，第二次领取不是过期接管
                self.assertFalse(events[2]["payload"]["takeover"])
                self.assertIsNone(events[2]["payload"]["previous_owner"])
            finally:
                restarted.close()


if __name__ == "__main__":
    unittest.main()
