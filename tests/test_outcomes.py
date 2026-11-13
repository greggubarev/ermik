"""Outcome attribution across separate controller and maintainer installations."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from ermik.config import Config
from ermik import state
from ermik.evidence_store import write_evidence
from ermik.outcomes import audit_cohort, build_outcome_report, build_quality_snapshot
from ermik.providers import ModelCandidate
from ermik.routing_policy import audit_route_order


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

    def test_task_set_groups_remain_distinct_and_legacy_is_unknown(self):
        for issue in (1, 2, 3):
            self.add_job(issue, issue + 10, "route:fast", "b" * 40)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET task_set='parser' WHERE issue IN (1,2)")
        report = build_outcome_report(self.cfg, self.maintainer)
        self.assertEqual([row["task_set"] for row in report["jobs"]],
                         ["parser", "parser", None])
        self.assertEqual({(row["task_set"], row["jobs"]) for row in report["cohorts"]},
                         {("parser", 2), (None, 1)})

    def attempt(self, issue, name, code, *, cost=None, tokens=None, usage=None,
                phase="initial", pending=False, provider="provider"):
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET attempts_count=attempts_count+1 WHERE issue=?",
                         (issue,))
            state.begin_attempt(conn, name, issue, provider, "model", phase)
            if not pending:
                metric = (usage if usage is not None else
                          {"complete": True, "input": tokens, "output": 0,
                           "reasoning": 0, "cache_read": 0, "cache_write": 0,
                           "total": tokens, "estimated_cost": cost}
                          if tokens is not None else None)
                state.finish_attempt(conn, name, metric, code, 10)

    def test_usage_shares_include_failed_retry_and_correction_attempts(self):
        head = "b" * 40
        self.add_job(1, 11, "route:fast", head)
        self.add_job(2, 12, "route:steady", head)
        self.attempt(1, "failed", 75, cost=0.1, usage={
            "complete": True, "input": 10, "output": 2, "reasoning": 1,
            "cache_read": 8, "cache_write": 1, "total": 22,
            "estimated_cost": 0.1})
        self.attempt(1, "retry", 0, cost=0.2, phase="initial", usage={
            "complete": True, "input": 20, "output": 4, "reasoning": 2,
            "cache_read": 12, "cache_write": 2, "total": 40,
            "estimated_cost": 0.2})
        self.attempt(1, "correction", 0, cost=0.3, phase="correction", usage={
            "complete": True, "input": 30, "output": 6, "reasoning": 3,
            "cache_read": 20, "cache_write": 3, "total": 62,
            "estimated_cost": 0.3})
        self.attempt(2, "steady", 0, cost=0.4, usage={
            "complete": True, "input": 5, "output": 5, "reasoning": 0,
            "cache_read": 0, "cache_write": 0, "total": 10,
            "estimated_cost": 0.4})
        with state.connect(self.cfg.state_dir) as conn:
            state.event(conn, 1, "retry_started")
        self.journal(1, 11, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        self.journal(2, 12, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        report = build_outcome_report(self.cfg, self.maintainer)
        job, fast = report["jobs"][0], report["cohorts"][0]
        for row in (job, fast):
            self.assertEqual(tuple(row[f"observed_{name}_tokens"] for name in
                                   ("input", "output", "reasoning", "cache_read",
                                    "cache_write", "total")),
                             (60, 12, 6, 40, 6, 124))
            self.assertEqual((row["cache_read_share_denominator_tokens"],
                              row["cache_read_share"],
                              row["output_share_denominator_tokens"],
                              row["output_share"], row["unknown_token_attempts"]),
                             (100, 0.4, 124, 12 / 124, 0))
        self.assertEqual((fast["retry_started_events"], fast["correction_attempts"],
                          fast["estimated_cost_per_accepted_pr"]), (1, 1, 0.6))
        self.assertEqual((report["cohorts"][1]["route"],
                          report["cohorts"][1]["cache_read_share"]),
                         ("route:steady", 0.0))

    def test_partial_usage_makes_shares_unknown_but_preserves_observed_counts(self):
        head = "b" * 40
        self.add_job(1, 11, "route:fast", head)
        self.attempt(1, "known", 0, cost=0.1, usage={
            "complete": True, "input": 10, "output": 2, "reasoning": 0,
            "cache_read": 5, "cache_write": 0, "total": 17,
            "estimated_cost": 0.1})
        self.attempt(1, "partial", 1, cost=0.2, usage={
            "complete": True, "input": 10, "output": 2, "reasoning": 0,
            "cache_write": 0, "total": 12, "estimated_cost": 0.2})
        self.journal(1, 11, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        report = build_outcome_report(self.cfg, self.maintainer)
        for row in (report["jobs"][0], report["cohorts"][0]):
            self.assertEqual((row["observed_input_tokens"],
                              row["observed_cache_read_tokens"],
                              row["unknown_token_attempts"]), (10, 5, 1))
            self.assertIsNone(row["cache_read_share"])
            self.assertIsNone(row["output_share"])
        self.assertEqual(report["cohorts"][0]["estimated_cost_per_accepted_pr"], 0.3)

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
        self.assertIsNone(row["cache_read_share"])
        self.assertIsNone(group["output_share"])
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
        self.assertIsNone(group["cache_read_share"])
        self.assertIsNone(group["output_share"])

    def test_zero_token_denominators_have_unknown_shares(self):
        head = "b" * 40
        self.add_job(1, 11, "route:fast", head)
        self.attempt(1, "empty", 0, cost=0, tokens=0)
        self.journal(1, 11, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        report = build_outcome_report(self.cfg, self.maintainer)
        for row in (report["jobs"][0], report["cohorts"][0]):
            self.assertEqual((row["unknown_token_attempts"],
                              row["cache_read_share_denominator_tokens"],
                              row["output_share_denominator_tokens"]), (0, 0, 0))
            self.assertIsNone(row["cache_read_share"])
            self.assertIsNone(row["output_share"])

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

    def test_quality_snapshot_counts_only_confirmed_single_provider_merges(self):
        head, old = "b" * 40, "c" * 40
        for issue in (1, 2):
            self.add_job(issue, issue + 10, "route:fast", head)
            self.attempt(issue, f"attempt-{issue}", 0, provider="provider-a")
        self.journal(1, 11, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        self.journal(2, 12, head, [(old, "reviewed"),
                                   (old, "changes_requested"),
                                   (head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        observed_at = datetime(2026, 10, 5, 4, 30, tzinfo=timezone(timedelta(hours=4)))
        self.assertEqual(build_quality_snapshot(self.cfg, self.maintainer,
                                                observed_at=observed_at), {
            "version": 1, "repo": self.cfg.repo,
            "observed_at": "2026-10-05T00:30:00+00:00",
            "providers": {"provider-a": {"samples": 2,
                                          "first_review_acceptances": 1,
                                          "quality_score": 0.5}},
        })

    def test_quality_snapshot_excludes_stale_missing_and_mixed_provider_jobs(self):
        old, head = "b" * 40, "c" * 40
        for issue in (1, 2, 3):
            self.add_job(issue, issue + 10, "route:fast", head)
            self.attempt(issue, f"attempt-{issue}", 0, provider="provider-a")
        self.journal(1, 11, old, [(old, "reviewed"), (old, "merge_intent"),
                                  (old, "merged")])
        self.journal(3, 13, head, [(head, "reviewed"), (head, "merge_intent"),
                                   (head, "merged")])
        self.attempt(3, "fallback", 0, provider="provider-b")
        self.assertEqual(build_quality_snapshot(self.cfg, self.maintainer,
                                                observed_at=datetime.now(timezone.utc))
                         ["providers"], {})

    def test_quality_snapshot_excludes_pending_incomplete_and_unattributed_attempts(self):
        head = "b" * 40
        for issue in (1, 2, 3):
            self.add_job(issue, issue + 10, "route:fast", head)
            self.attempt(issue, f"attempt-{issue}", 0, provider="provider-a")
            self.journal(issue, issue + 10, head,
                         [(head, "reviewed"), (head, "merge_intent"),
                          (head, "merged")])
        self.attempt(1, "pending", -2, pending=True)
        with state.connect(self.cfg.state_dir) as conn:
            conn.execute("UPDATE jobs SET attempts_count=2 WHERE issue=2")
        self.attempt(3, "unattributed", 0, provider="")
        self.assertEqual(build_quality_snapshot(self.cfg, self.maintainer)
                         ["providers"], {})
        with self.assertRaisesRegex(ValueError, "timezone"):
            build_quality_snapshot(self.cfg, self.maintainer,
                                   observed_at=datetime(2026, 10, 5))

    def test_quality_snapshot_configured_provider_without_samples_is_unknown(self):
        candidate = ModelCandidate("unused", "model", "UNUSED_API_KEY",
                                   base_url="https://example.com")
        cfg = replace(self.cfg, models=(candidate,))
        self.add_job(1, 11, "route:fast", "b" * 40)
        self.assertEqual(build_quality_snapshot(cfg, self.maintainer)["providers"], {
            "unused": {"samples": 0, "first_review_acceptances": 0,
                       "quality_score": None},
        })

    def test_route_audit_binds_exact_cohort_and_refuses_changed_or_stale_evidence(self):
        head = "b" * 40
        candidates = (ModelCandidate("one", "model", "ONE_API_KEY",
                                     base_url="https://example.com"),
                      ModelCandidate("two", "model", "TWO_API_KEY",
                                     base_url="https://example.com"))
        cfg = replace(self.cfg, models=candidates, quality_min_samples=2,
                      quality_max_age_seconds=3600)
        for issue in range(1, 5):
            self.add_job(issue, issue + 10, "route:auto", head)
            self.attempt(issue, f"attempt-{issue}", 0,
                         provider="one" if issue < 3 else "two")
            journal = ([(head, "reviewed"), (head, "merge_intent"), (head, "merged")]
                       if issue >= 3 else
                       [("c" * 40, "reviewed"), ("c" * 40, "changes_requested"),
                        (head, "reviewed"), (head, "merge_intent"), (head, "merged")])
            self.journal(issue, issue + 10, head, journal)
        now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        report = build_outcome_report(cfg, self.maintainer)
        quality = build_quality_snapshot(cfg, self.maintainer, observed_at=now,
                                         source_report=report)
        audit = {**quality, "version": 2, "cohort": audit_cohort(report),
                 "thresholds": {"min_samples": 2, "max_age_seconds": 3600},
                 "gates": {"admission": "required", "checks": "required",
                           "maintainer_review": "required"}}
        path = cfg.state_dir.parent / "audit.json"
        path.write_text(json.dumps(audit), encoding="utf-8")
        result = audit_route_order(cfg, report, path, candidates, now)
        self.assertEqual(result["decision"], "audited")
        self.assertEqual(result["proposed_order"], [candidates[1].ref, candidates[0].ref])
        self.assertEqual(len(result["cohort"]["accepted_prs"]), 4)
        self.assertEqual(result["cohort"]["accepted_prs"][0]["head"], head)
        audit["thresholds"]["min_samples"] = 20
        path.write_text(json.dumps(audit), encoding="utf-8")
        sparse = audit_route_order(replace(cfg, quality_min_samples=20), report,
                                   path, candidates, now)
        self.assertEqual(sparse["decision"], "refused")
        self.assertEqual(sparse["refusal_reason"],
                         "insufficient_or_stale_measurements")
        self.assertEqual(sparse["provider_samples"], {"one": 2, "two": 2})
        self.assertEqual(sparse["cohort"], audit_cohort(report))
        self.assertEqual(sparse["observed_at"], now.isoformat())
        self.assertEqual(sparse["proposed_order"], [item.ref for item in candidates])
        audit["thresholds"]["min_samples"] = 2
        audit["cohort"]["accepted_prs"][0]["issue"] = 99
        path.write_text(json.dumps(audit), encoding="utf-8")
        refused = audit_route_order(cfg, report, path, candidates, now)
        self.assertEqual((refused["decision"], refused["proposed_order"]),
                         ("refused", [item.ref for item in candidates]))
        self.assertNotIn("cohort", refused)
        audit["cohort"] = audit_cohort(report)
        path.write_text(json.dumps(audit), encoding="utf-8")
        changed_report = json.loads(json.dumps(report))
        changed_report["jobs"][0]["route"] = "route:changed"
        self.assertEqual(audit_route_order(cfg, changed_report, path, candidates, now)
                         ["decision"], "refused")
        audit["cohort"] = audit_cohort(report)
        audit["observed_at"] = (now - timedelta(seconds=3601)).isoformat()
        path.write_text(json.dumps(audit), encoding="utf-8")
        self.assertEqual(audit_route_order(cfg, report, path, candidates, now)["decision"],
                         "refused")
        audit["observed_at"] = now.isoformat()
        audit["gates"]["checks"] = "skipped"
        path.write_text(json.dumps(audit), encoding="utf-8")
        self.assertEqual(audit_route_order(cfg, report, path, candidates, now)["decision"],
                         "refused")
        audit["gates"]["checks"] = "required"
        audit["thresholds"]["min_samples"] = 1
        path.write_text(json.dumps(audit), encoding="utf-8")
        self.assertEqual(audit_route_order(cfg, report, path, candidates, now)["decision"],
                         "refused")
        audit["thresholds"]["min_samples"] = 2
        audit["gates"]["checks"] = "required"
        audit["observed_at"] = "private invalid date"
        path.write_text(json.dumps(audit), encoding="utf-8")
        refusal = audit_route_order(cfg, report, path, candidates, now)
        self.assertEqual(refusal["refusal_reason"], "invalid_or_incomplete_evidence")
        self.assertNotIn("private", json.dumps(refusal))


if __name__ == "__main__":
    unittest.main()
