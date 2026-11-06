"""Outcome attribution across separate controller and maintainer installations."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from ermik.config import Config
from ermik import state
from ermik.evidence_store import write_evidence
from ermik.outcomes import build_outcome_report


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.cfg = Config("owner/project", "keeper", root / "controller", root / "work",
                          "image:stable", ("run",), (("check",),), 1, 60, (), "main")
        self.maintainer = root / "maintainer"
        self.maintainer.mkdir()

    def add_job(self, issue, pr, route, head):
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("INSERT INTO admissions(issue,body_sha,allowed_json,admitted_at) "
                         "VALUES(?,?,?,?)", (issue, "a" * 64, '["src"]',
                                          "2026-10-01 00:00:00"))
            conn.execute("INSERT INTO jobs(issue,branch,base_sha,head_sha,pr,status,"
                         "allowed_json,route_key) VALUES(?,?,?,?,?,?,?,?)",
                         (issue, f"work/{issue}", "a" * 40, head, pr, "merged",
                          '["src"]', route))

    def attempt(self, issue, name, code, *, cost=None, tokens=None, phase="initial",
                pending=False, provider="provider"):
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET attempts_count=attempts_count+1 WHERE issue=?",
                         (issue,))
            state.begin_attempt(conn, name, issue, provider, "model", phase)
            if not pending:
                metric = ({"complete": True, "input": tokens, "output": 0,
                           "total": tokens, "estimated_cost": cost}
                          if tokens is not None else None)
                state.finish_attempt(conn, name, metric, code, 10)

    def journal(self, issue, pr, head, decisions):
        path = self.maintainer / "decision-trail" / f"pr-{pr}.json"
        path.parent.mkdir(exist_ok=True)
        entries = [{"repo": self.cfg.repo, "issue": issue, "pr": pr,
                    "base": "a" * 40, "head": decision_head,
                    "evidence_sha256": "e" * 64, "decision": decision,
                    "timestamp": f"2026-10-0{n + 2}T00:00:00+00:00"}
                   for n, (decision_head, decision) in enumerate(decisions)]
        write_evidence(path, {"version": 1, "repo": self.cfg.repo,
                              "pr": pr, "entries": entries})

    def test_two_routes_retry_and_failed_attempts_are_in_cost_numerator(self):
        head = "b" * 40
        self.add_job(1, 11, "route:fast", head)
        self.add_job(2, 12, "route:steady", head)
        self.add_job(3, 13, "route:fast", head)  # failed task still adds cost
        self.attempt(1, "one-failed", 75, cost=0.2, tokens=20)
        self.attempt(1, "one-retry", 0, cost=0.3, tokens=30, provider="fallback")
        self.attempt(2, "two", 0, cost=0.4, tokens=40)
        self.attempt(3, "three", 1, cost=0.5, tokens=50)
        with state.connect(self.cfg.state_dir) as conn:
            for kind in ("quota_paused", "retry_scheduled", "retry_started"):
                state.event(conn, 1, kind)
        self.journal(1, 11, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        self.journal(2, 12, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        report = build_outcome_report(self.cfg, self.maintainer, period="2026-10")
        fast, steady = report["cohorts"]
        self.assertEqual((fast["route"], fast["accepted_prs"], fast["attempts"],
                          fast["rate_limit_attempts"], fast["estimated_cost_per_accepted_pr"]),
                         ("route:fast", 1, 3, 1, 1.0))
        self.assertEqual((fast["first_review_rate_denominator"],
                          fast["first_review_acceptance_rate"],
                          fast["elapsed_observed_prs"], fast["elapsed_unknown_prs"],
                          fast["observed_mean_elapsed_ms"]),
                         (1, 1.0, 1, 0, 3 * 86400 * 1000))
        self.assertEqual((steady["route"], steady["accepted_prs"],
                          steady["estimated_cost_per_accepted_pr"]),
                         ("route:steady", 1, 0.4))
        self.assertTrue(report["jobs"][0]["first_review_accepted"])
        self.assertEqual((report["jobs"][0]["provider_switches"],
                          report["jobs"][0]["retry_started_events"],
                          report["jobs"][0]["quota_paused_events"]), (1, 1, 1))
        self.assertEqual(report["jobs"][0]["elapsed_ms"], 3 * 86400 * 1000)
        serialized = json.dumps(report)
        self.assertNotIn(str(self.cfg.state_dir), serialized)
        self.assertNotIn('"src"', serialized)

    def test_correction_distinct_by_head_and_stale_merge_rejected(self):
        old, new = "b" * 40, "c" * 40
        self.add_job(1, 11, "route:fast", new)
        self.journal(1, 11, new, [(old, "reviewed"), (old, "changes_requested"),
                                  (new, "reviewed"), (new, "merge_intent"),
                                  (new, "merged")])
        row = build_outcome_report(self.cfg, self.maintainer)["jobs"][0]
        self.assertTrue(row["accepted"])
        self.assertFalse(row["first_review_accepted"])
        self.assertEqual(row["distinct_corrections"], 1)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET head_sha=? WHERE issue=1", ("d" * 40,))
        row = build_outcome_report(self.cfg, self.maintainer)["jobs"][0]
        self.assertFalse(row["accepted"])
        self.assertEqual(row["decision_state"], "stale")

    def test_missing_journal_pending_usage_and_unknown_cost(self):
        self.add_job(1, 11, "route:fast", "b" * 40)
        self.attempt(1, "pending", -2, pending=True)
        row = build_outcome_report(self.cfg, self.maintainer)["jobs"][0]
        group = build_outcome_report(self.cfg, self.maintainer)["cohorts"][0]
        self.assertEqual(row["decision_state"], "missing")
        self.assertFalse(row["accepted"])
        self.assertEqual((row["pending_attempts"], row["unknown_token_attempts"],
                          row["unknown_cost_attempts"]), (1, 1, 1))
        self.assertIsNone(group["estimated_cost_per_accepted_pr"])
        self.assertIsNone(group["billed_cost"])
        self.assertEqual(group["first_review_rate_denominator"], 0)
        self.assertIsNone(group["first_review_acceptance_rate"])

    def test_accepted_without_admission_time_has_explicit_unknown_elapsed(self):
        head = "b" * 40
        self.add_job(1, 11, "route:fast", head)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("DELETE FROM admissions WHERE issue=1")
        self.attempt(1, "success", 0, cost=0.3, tokens=30)
        self.journal(1, 11, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        group = build_outcome_report(self.cfg, self.maintainer)["cohorts"][0]
        self.assertEqual((group["elapsed_observed_prs"], group["elapsed_unknown_prs"]),
                         (0, 1))
        self.assertIsNone(group["observed_mean_elapsed_ms"])

    def test_accepted_pr_with_unknown_attempt_cost_has_unavailable_ratio(self):
        head = "b" * 40
        self.add_job(1, 11, "route:fast", head)
        self.attempt(1, "failed-no-usage", 1)
        self.attempt(1, "success", 0, cost=0.3, tokens=30)
        self.journal(1, 11, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        group = build_outcome_report(self.cfg, self.maintainer)["cohorts"][0]
        self.assertEqual(group["accepted_prs"], 1)
        self.assertEqual(group["observed_estimated_cost"], 0.3)
        self.assertEqual(group["unknown_cost_attempts"], 1)
        self.assertIsNone(group["estimated_cost_per_accepted_pr"])

    def test_partial_attempt_rows_make_cost_unknown_even_when_observed_cost_complete(self):
        head = "b" * 40
        self.add_job(1, 11, "route:fast", head)
        self.attempt(1, "recorded", 0, cost=0.3, tokens=30)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET attempts_count=3 WHERE issue=1")
        self.journal(1, 11, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        report = build_outcome_report(self.cfg, self.maintainer)
        row, group = report["jobs"][0], report["cohorts"][0]
        self.assertEqual((row["expected_attempts"], row["attempts"],
                          row["missing_attempt_rows"]), (3, 1, 2))
        self.assertEqual((row["unknown_cost_attempts"],
                          row["unknown_token_attempts"]), (2, 2))
        self.assertFalse(row["attempt_history_complete"])
        self.assertEqual(group["missing_attempt_history_jobs"], 1)
        self.assertEqual(group["missing_attempt_rows"], 2)
        self.assertEqual(group["observed_estimated_cost"], 0.3)
        self.assertIsNone(group["estimated_cost_per_accepted_pr"])

    def test_re_reviewed_head_is_not_first_review_acceptance(self):
        old, new = "b" * 40, "c" * 40
        self.add_job(1, 11, "route:fast", new)
        self.journal(1, 11, new, [(old, "reviewed"), (new, "reviewed"),
                                  (new, "merge_intent"), (new, "merged")])
        row = build_outcome_report(self.cfg, self.maintainer)["jobs"][0]
        self.assertTrue(row["accepted"])
        self.assertFalse(row["first_review_accepted"])

    def test_invalid_journal_and_same_state_rejected(self):
        self.add_job(1, 11, "route:fast", "b" * 40)
        path = self.maintainer / "decision-trail" / "pr-11.json"
        path.parent.mkdir()
        path.write_text('{"version":1,"repo":"other/repo"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            build_outcome_report(self.cfg, self.maintainer)
        with self.assertRaisesRegex(ValueError, "separate"):
            build_outcome_report(self.cfg, self.cfg.state_dir)


if __name__ == "__main__":
    unittest.main()
