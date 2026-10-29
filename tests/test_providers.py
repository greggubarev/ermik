"""Provider selection, isolation and failure routing without real credentials."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
import runpy
import json
import os
import tempfile
from unittest.mock import patch

from ermik.providers import (ModelCandidate, candidate_from_dict, choose_model,
                                claim_model, classify_failure, opencode_command,
                                record_exhaustion, release_model, model_spec)


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

    def test_only_provider_status_triggers_fallback(self):
        self.assertEqual(classify_failure('{"type":"text","text":"HTTP 429"}', "", 1), "other")
        self.assertEqual(classify_failure('{"type":"error","error":{"status":429}}', "", 1), "quota")
        self.assertEqual(classify_failure('{"type":"error","error":{"statusCode":401}}', "", 1), "auth")
        self.assertEqual(classify_failure("", "HTTP 429 Too Many Requests", 1), "quota")
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
