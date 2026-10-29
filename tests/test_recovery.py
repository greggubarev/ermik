"""Durable retry transitions without external writes."""

from pathlib import Path
import tempfile
import unittest

from ermik import recovery, state


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        with state.connect(self.root) as conn:
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,status,allowed_json) "
                         "VALUES(1,'work/issue-1','a','running','[\"src\"]')")
        self.policy = recovery.RetryPolicy(max_attempts=3, base_delay_seconds=10,
                                            max_delay_seconds=100, lease_seconds=20)

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_failure_does_not_consume_attempt(self):
        with state.connect(self.root) as conn:
            first = recovery.record_failure(conn, 1, "transient", failure_id="run-1",
                                            resume_status="running", now=100,
                                            policy=self.policy)
            second = recovery.record_failure(conn, 1, "transient", failure_id="run-1",
                                             resume_status="running", now=200,
                                             policy=self.policy)
            self.assertEqual(first, second)
            self.assertEqual((first.status, first.attempts, first.next_retry_at),
                             ("retry_wait", 1, 110))
            self.assertTrue(state.conflicting(conn, ("src/a",)))

    def test_retry_claim_requires_verification_and_token(self):
        with state.connect(self.root) as conn:
            recovery.record_failure(conn, 1, "resource", failure_id="run-1",
                                    resume_status="running", now=100,
                                    policy=self.policy)
            self.assertEqual(recovery.claim_due(conn, now=109, policy=self.policy), [])
            claim = recovery.claim_due(conn, now=110, policy=self.policy)[0]
            self.assertEqual(state.job(conn, 1)["status"], "retry_wait")
            self.assertEqual(recovery.claim_due(conn, now=110, policy=self.policy), [])
            self.assertTrue(recovery.activate_claim(conn, claim, now=111))
            self.assertFalse(recovery.activate_claim(conn, claim, now=111))
            with self.assertRaisesRegex(ValueError, "token required"):
                recovery.record_failure(conn, 1, "transient", failure_id="run-2",
                                        resume_status="running", now=112,
                                        policy=self.policy)
            decision = recovery.record_failure(conn, 1, "transient",
                                               failure_id="run-2", resume_status="running",
                                               lease_token=claim.token, now=112,
                                               policy=self.policy)
            self.assertEqual((decision.attempts, decision.next_retry_at), (2, 132))

    def test_expired_lease_consumes_attempt_then_stops(self):
        with state.connect(self.root) as conn:
            recovery.record_failure(conn, 1, "interrupted", failure_id="run-1",
                                    resume_status="running", now=100,
                                    policy=self.policy)
            first = recovery.claim_due(conn, now=110, policy=self.policy)[0]
        with state.connect(self.root) as conn:  # Simulate a restarted process.
            self.assertEqual(recovery.claim_due(conn, now=130, policy=self.policy), [])
            self.assertFalse(recovery.activate_claim(conn, first, now=130))
            second = recovery.claim_due(conn, now=150, policy=self.policy)[0]
            self.assertEqual(second.attempts, 3)
        with state.connect(self.root) as conn:
            self.assertEqual(recovery.claim_due(conn, now=170, policy=self.policy), [])
            self.assertEqual(state.job(conn, 1)["status"], "blocked")

    def test_terminal_failure_is_not_requeued(self):
        with state.connect(self.root) as conn:
            result = recovery.record_failure(conn, 1, "identity", failure_id="run-1",
                                             resume_status="running", now=100,
                                             policy=self.policy)
            self.assertEqual((result.status, result.next_retry_at), ("blocked", None))
            self.assertEqual(recovery.claim_due(conn, now=1000, policy=self.policy), [])

    def test_provider_quota_wait_does_not_terminally_exhaust_job(self):
        with state.connect(self.root) as conn:
            decision = recovery.record_failure(conn, 1, "provider", failure_id="quota-1",
                                               resume_status="running", now=100,
                                               policy=self.policy)
            for index in range(2, 6):
                claim = recovery.claim_due(conn, now=decision.next_retry_at,
                                           policy=self.policy)[0]
                self.assertTrue(recovery.activate_claim(conn, claim,
                                                        now=decision.next_retry_at))
                decision = recovery.record_failure(conn, 1, "provider",
                                                   failure_id=f"quota-{index}",
                                                   resume_status="running",
                                                   lease_token=claim.token,
                                                   now=decision.next_retry_at,
                                                   policy=self.policy)
            self.assertEqual(decision.status, "retry_wait")
            self.assertGreater(decision.attempts, self.policy.max_attempts)

    def test_crash_after_activation_fails_closed(self):
        with state.connect(self.root) as conn:
            recovery.record_failure(conn, 1, "transient", failure_id="run-1",
                                    resume_status="running", now=100,
                                    policy=self.policy)
            claim = recovery.claim_due(conn, now=110, policy=self.policy)[0]
            self.assertTrue(recovery.activate_claim(conn, claim, now=111))
        with state.connect(self.root) as conn:
            self.assertEqual(recovery.claim_due(conn, now=130, policy=self.policy), [])
            self.assertEqual(state.job(conn, 1)["status"], "blocked")
            self.assertFalse(recovery.activate_claim(conn, claim, now=131))

    def test_failed_reconciliation_blocks_claim_before_activation(self):
        with state.connect(self.root) as conn:
            recovery.record_failure(conn, 1, "transient", failure_id="run-1",
                                    resume_status="running", now=100,
                                    policy=self.policy)
            claim = recovery.claim_due(conn, now=110, policy=self.policy)[0]
            result = recovery.record_failure(conn, 1, "identity", failure_id="verify-2",
                                             resume_status="running", lease_token=claim.token,
                                             now=111, policy=self.policy)
            self.assertEqual(result.status, "blocked")
            self.assertFalse(recovery.activate_claim(conn, claim, now=112))

    def test_success_clears_retry_only_after_durable_success(self):
        with state.connect(self.root) as conn:
            recovery.record_failure(conn, 1, "transient", failure_id="run-1",
                                    resume_status="correcting", now=100,
                                    policy=self.policy)
            claim = recovery.claim_due(conn, now=110, policy=self.policy)[0]
            self.assertFalse(recovery.complete_claim(conn, claim))
            self.assertTrue(recovery.activate_claim(conn, claim, now=111))
            conn.execute("UPDATE jobs SET status='awaiting_review' WHERE issue=1")
            self.assertTrue(recovery.complete_claim(conn, claim))
            self.assertFalse(recovery.complete_claim(conn, claim))
            self.assertEqual(recovery.claim_due(conn, now=1000, policy=self.policy), [])


if __name__ == "__main__":
    unittest.main()
