"""Provider selection, isolation and failure routing without real credentials."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
import runpy
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from ermik.providers import (ModelCandidate, candidate_from_dict, choose_model,
                                claim_model, claim_model_decision, available_capacity,
                                classify_failure, opencode_command,
                                record_exhaustion, release_model, model_spec)
from ermik import state


KIMI = ModelCandidate("kimi-code", "k3-256k", "KIMI_CODE_API_KEY")
GO = ModelCandidate("opencode-go", "kimi-k3", "OPENCODE_GO_API_KEY")
FREE = ModelCandidate("free-account-1", "coder-small", "FREE_ONE_API_KEY",
                      "https://api.example.net/v1")


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_distinct_identity_and_strict_config(self):
        self.assertEqual(KIMI.ref, "kimi-code/k3-256k")
        self.assertEqual(GO.ref, "opencode-go/kimi-k3")
        self.assertEqual(candidate_from_dict({"provider_id": "opencode-go",
            "model_id": "kimi-k3", "credential_env": "OPENCODE_GO_API_KEY"}), GO)
        with self.assertRaises(ValueError):
            ModelCandidate("opencode-go", "kimi-k3", "KIMI_CODE_API_KEY")
        with self.assertRaises(ValueError):
            ModelCandidate("kimi-code", "k3-256k", "KIMI_CODE_API_KEY",
                           base_url="https://example.invalid/v1")
        with self.assertRaises(ValueError):
            candidate_from_dict({"provider_id": "opencode-go", "model_id": "kimi-k3",
                                 "credential_env": "OPENCODE_GO_API_KEY", "secret": "value"})

    def test_custom_account_spec_round_trip_and_pool(self):
        self.assertEqual(candidate_from_dict(json.loads(model_spec(FREE))), FREE)
        self.assertNotIn("secret-value", model_spec(FREE))
        accounts = [ModelCandidate(f"free-account-{index}", "coder-small",
                                   f"FREE_{index}_API_KEY", f"https://api{index}.example.net/v1")
                    for index in range(10)]
        claimed = [claim_model(self.conn, accounts, f"issue-{index}", 1000)
                   for index in range(10)]
        self.assertEqual(claimed, accounts)
        self.assertIsNone(claim_model(self.conn, accounts, "issue-10", 1000))
        record_exhaustion(self.conn, accounts[0], 1100, now=1000)
        self.assertEqual(choose_model(self.conn, accounts, now=1001), accounts[1])

    def test_custom_account_rejects_unsafe_specs(self):
        for url in ("http://api.example.net/v1", "https://user:pass@api.example.net/v1",
                    "https://api.example.net/v1?key=x", "https://localhost/v1",
                    "https://127.0.0.1/v1", "https://api.example.net/v1#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ModelCandidate("free-account", "coder", "FREE_API_KEY", url)
        for env in ("GITHUB_TOKEN", "GH_TOKEN", "GIT_CREDENTIAL", "SSH_KEY",
                    "ERMIK_SECRET", "OPENCODE_AUTH", "lowercase_key", "PATH"):
            with self.subTest(env=env), self.assertRaises(ValueError):
                ModelCandidate("free-account", "coder", env, "https://api.example.net/v1")
        with self.assertRaises(ValueError):
            ModelCandidate("free-account", "coder", "FREE_API_KEY",
                           "https://api.example.net/v1", "@evil/plugin")

    def test_quota_cooldown_switches_then_recovers(self):
        future = 2000
        record_exhaustion(self.conn, KIMI, future, now=1000)
        other_kimi = ModelCandidate("kimi-code", "kimi-for-coding", "KIMI_CODE_API_KEY")
        self.assertEqual(choose_model(self.conn, [KIMI, other_kimi, GO], now=future - 1), GO)
        self.assertEqual(choose_model(self.conn, [KIMI, GO], now=future), KIMI)
        with self.assertRaises(ValueError):
            record_exhaustion(self.conn, GO, future, reason="auth", now=1000)
        with self.assertRaises(ValueError):
            record_exhaustion(self.conn, GO, 90000, now=1000)

    def test_claims_distribute_parallel_jobs_and_release(self):
        now = 2_000_000_000
        self.assertEqual(claim_model(self.conn, [KIMI, GO], "issue-1", now), KIMI)
        self.assertEqual(claim_model(self.conn, [KIMI, GO], "issue-2", now), GO)
        self.assertIsNone(claim_model(self.conn, [KIMI, GO], "issue-3", now))
        release_model(self.conn, "issue-1")
        self.assertEqual(claim_model(self.conn, [KIMI, GO], "issue-3", now), KIMI)
        with self.assertRaises(ValueError):
            claim_model(self.conn, [KIMI, GO], "issue-3", now)

    def test_expired_claim_is_reclaimed(self):
        self.assertEqual(claim_model(self.conn, [KIMI], "issue-1", 1000, 10), KIMI)
        self.assertEqual(claim_model(self.conn, [KIMI], "issue-2", 1010, 10), KIMI)

    def test_provider_capacity_cannot_vary_by_model(self):
        other = ModelCandidate("kimi-code", "kimi-for-coding", "KIMI_CODE_API_KEY",
                               max_parallel=2)
        with self.assertRaises(ValueError):
            claim_model(self.conn, [KIMI, other], "issue-1", 1000)

    def test_budget_ceiling_reserves_cost_and_reports_fallback(self):
        now = int(datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp())
        policies = {KIMI.provider_id: SimpleNamespace(estimated_budget_ceiling=2.0,
                                                     attempt_reservation=1.25)}
        first = claim_model_decision(self.conn, [KIMI, GO], "lease-1",
                                     policies=policies, route_key="r", now=now)
        self.assertEqual((first["chosen"], first["reason"]), (KIMI, "priority"))
        self.assertEqual(first["eligible"], (KIMI.ref, GO.ref))
        self.assertEqual(self.conn.execute("SELECT route_key,reserved_cost FROM "
                                           "provider_leases").fetchone(), ("r", 1.25))
        second = claim_model_decision(self.conn, [KIMI, GO], "lease-2",
                                      policies=policies, now=now)
        self.assertEqual((second["chosen"], second["reason"], second["eligible"]),
                         (GO, "budget", (GO.ref,)))
        self.assertEqual(available_capacity(self.conn, [KIMI, GO], now, policies), 0)
        release_model(self.conn, "lease-1")
        self.assertEqual(choose_model(self.conn, [KIMI, GO], now, policies), KIMI)

    def test_unknown_monthly_usage_fails_closed(self):
        now = int(datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp())
        policies = {KIMI.provider_id: SimpleNamespace(estimated_budget_ceiling=2.0,
                                                     attempt_reservation=1.0)}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with state.connect(root) as conn:
                conn.execute("INSERT INTO usage_events(attempt_id,issue,provider_id,"
                             "model_id,complete,result_code,created_at) "
                             "VALUES('a',1,?, 'm',0,75,'2026-11-02 00:00:00')",
                             (KIMI.provider_id,))
            with state.connect(root) as conn:
                decision = claim_model_decision(conn, [KIMI, GO], "lease-1",
                                                policies=policies, now=now)
                self.assertEqual((decision["chosen"], decision["reason"]),
                                 (GO, "budget"))
                self.assertEqual(available_capacity(conn, [KIMI], now, policies), 0)
                conn.execute("UPDATE usage_events SET estimated_cost=0.8 "
                             "WHERE attempt_id='a'")
                self.assertEqual(available_capacity(conn, [KIMI], now, policies), 1)
                conn.execute("UPDATE usage_events SET estimated_cost=1.2 "
                             "WHERE attempt_id='a'")
                self.assertEqual(available_capacity(conn, [KIMI], now, policies), 0)

    def test_concurrent_budget_reservations_cannot_exceed_ceiling(self):
        now = int(datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp())
        policies = {KIMI.provider_id: SimpleNamespace(estimated_budget_ceiling=1.0,
                                                     attempt_reservation=1.0)}
        with tempfile.TemporaryDirectory() as directory:
            db = state.initialize(Path(directory))
            first = sqlite3.connect(db)
            second = sqlite3.connect(db)
            try:
                decision = claim_model_decision(first, [KIMI], "lease-1",
                                                policies=policies, now=now)
                self.assertEqual(decision["chosen"], KIMI)
                decision = claim_model_decision(second, [KIMI], "lease-2",
                                                policies=policies, now=now)
                self.assertEqual((decision["chosen"], decision["reason"]),
                                 (None, "budget"))
                self.assertEqual(first.execute("SELECT sum(reserved_cost) FROM "
                                               "provider_leases").fetchone()[0], 1.0)
            finally:
                first.close()
                second.close()

    def test_active_pending_attempt_uses_lease_reservation(self):
        now = int(datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp())
        parallel = ModelCandidate("kimi-code", "k3-256k", "KIMI_CODE_API_KEY",
                                  max_parallel=2)
        policies = {parallel.provider_id: SimpleNamespace(
            estimated_budget_ceiling=2.0, attempt_reservation=1.0)}
        with tempfile.TemporaryDirectory() as directory:
            with state.connect(Path(directory)) as conn:
                first = claim_model_decision(conn, [parallel], "lease-1",
                                             policies=policies, now=now)
                self.assertEqual(first["chosen"], parallel)
                conn.execute("INSERT INTO usage_events(attempt_id,issue,provider_id,"
                             "model_id,complete,result_code,created_at) "
                             "VALUES('lease-1',1,?, 'k3-256k',0,-2,"
                             "'2026-11-07 00:00:00')", (parallel.provider_id,))
                second = claim_model_decision(conn, [parallel], "lease-2",
                                              policies=policies, now=now)
                self.assertEqual(second["chosen"], parallel)
                self.assertEqual(available_capacity(conn, [parallel], now, policies), 0)
                conn.execute("UPDATE usage_events SET result_code=0,"
                             "finished_at='2026-11-07 00:01:00' WHERE attempt_id='lease-1'")
                release_model(conn, "lease-1")
                self.assertEqual(available_capacity(conn, [parallel], now, policies), 0)

    def test_no_eligible_candidates_decision(self):
        decision = claim_model_decision(self.conn, [], "lease-1", now=1000)
        self.assertEqual(decision, {"chosen": None, "eligible": (),
                                    "reason": "no_eligible"})

    def test_only_provider_status_triggers_fallback(self):
        self.assertEqual(classify_failure('{"type":"text","text":"HTTP 429"}', "", 1), "other")
        self.assertEqual(classify_failure('{"type":"error","error":{"status":429}}', "", 1), "quota")
        self.assertEqual(classify_failure('{"type":"error","error":{"statusCode":401}}', "", 1), "auth")
        self.assertEqual(classify_failure("", "HTTP 429 Too Many Requests", 1), "quota")
        self.assertEqual(classify_failure("", "request failed with status 429", 1), "quota")
        self.assertEqual(classify_failure("", "request failed with status 4290", 1), "other")
        self.assertEqual(classify_failure("", "worker exited with exit status 429", 1), "other")
        self.assertEqual(classify_failure("", "authentication failed", 1), "other")
        self.assertEqual(classify_failure("", "", 75), "quota")
        self.assertEqual(classify_failure('{"type":"error","properties":{"error":{"data":{"statusCode":"429"}}}}', "", 1), "quota")

    def test_worker_config_uses_builtin_go_and_scoped_kimi(self):
        worker = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                    "examples" / "opencode-worker.py"))
        with patch.dict(os.environ, {"KIMI_CODE_API_KEY": "kimi-test-secret",
                                      "OPENCODE_GO_API_KEY": "go-test-secret"}):
            kimi = worker["_config"](KIMI)
            go = worker["_config"](GO)
        self.assertEqual(kimi["provider"]["kimi-code"]["options"]["baseURL"],
                         "https://api.kimi.ai/coding/v1")
        self.assertEqual(kimi["provider"]["kimi-code"]["npm"],
                         "@ai-sdk/openai-compatible")
        self.assertEqual(go["provider"]["opencode-go"],
                         {"options": {"apiKey": "go-test-secret"}})
        self.assertEqual(kimi["permission"]["bash"], "deny")
        self.assertEqual(go["permission"]["external_directory"], "deny")

    def test_worker_requires_valid_custom_spec_and_ref_match(self):
        worker = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                    "examples" / "opencode-worker.py"))
        with patch.dict(os.environ, {"ERMIK_MODEL_REF": FREE.ref,
                                      "ERMIK_MODEL_SPEC": model_spec(FREE),
                                      "FREE_ONE_API_KEY": "selected-secret"}):
            self.assertEqual(worker["_selected"](), FREE)
            config = worker["_config"](FREE)
            self.assertEqual(config["provider"][FREE.provider_id]["options"],
                             {"baseURL": FREE.endpoint, "apiKey": "selected-secret"})
        with patch.dict(os.environ, {"ERMIK_MODEL_REF": "free-account-1/other",
                                      "ERMIK_MODEL_SPEC": model_spec(FREE)}):
            with self.assertRaises(ValueError):
                worker["_selected"]()
        with patch.dict(os.environ, {"ERMIK_MODEL_REF": FREE.ref}, clear=True):
            with self.assertRaises(ValueError):
                worker["_selected"]()
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "opencode.json"
            with patch.dict(os.environ, {"FREE_ONE_API_KEY": "selected-secret"}):
                config_path.write_text(json.dumps(worker["_config"](FREE)), encoding="utf-8")
            with patch.dict(os.environ, {"FREE_ONE_API_KEY": "selected-secret",
                                      "KIMI_CODE_API_KEY": "other-secret",
                                      "OPENCODE_GO_API_KEY": "go-secret",
                                      "GITHUB_TOKEN": "github-secret",
                                      "ERMIK_MODEL_SPEC": model_spec(FREE)}):
                child = worker["_child_environment"](FREE, config_path)
            self.assertEqual(child["FREE_ONE_API_KEY"], "selected-secret")
            for name in ("KIMI_CODE_API_KEY", "OPENCODE_GO_API_KEY", "GITHUB_TOKEN",
                         "ERMIK_MODEL_SPEC"):
                self.assertNotIn(name, child)
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", child)

    def test_command_is_argument_vector(self):
        command = opencode_command(GO, "Fix one file")
        self.assertEqual(command[:5], ("opencode", "run", "--model",
                                       "opencode-go/kimi-k3", "--agent"))
        self.assertEqual(command[-2], "--")
        self.assertEqual(command[-1], "Fix one file")


if __name__ == "__main__":
    unittest.main()
