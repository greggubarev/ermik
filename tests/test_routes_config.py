"""Configured issue routes must resolve only to trusted providers."""

import json
from pathlib import Path
import tempfile
import unittest

from ermik.config import load_config


class RouteConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.path = root / "config.json"
        self.raw = {
            "repo": "owner/repo", "maintainer": "owner",
            "state_dir": str(root / "state"), "workspace": str(root / "work"),
            "image": "worker:test", "worker_command": ["python3", "worker.py"],
            "checks": [["true"]], "max_parallel": 1, "poll_seconds": 60,
            "models": [{"provider_id": "kimi-code", "model_id": "k3-256k",
                        "credential_env": "KIMI_CODE_API_KEY",
                        "supported_efforts": ["low", "medium", "high"]}],
        }

    def tearDown(self):
        self.temp.cleanup()

    def load(self, routes=None):
        raw = dict(self.raw)
        if routes is not None:
            raw["routes"] = routes
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        return load_config(self.path)

    def test_optional_routes_default_to_empty_map(self):
        self.assertEqual(self.load().routes, {})
        self.assertEqual(self.load().complexity_profiles, {})

    def test_complexity_profiles_select_configured_model_and_effort(self):
        ref = "kimi-code/k3-256k"
        self.raw["complexity_profiles"] = {
            tier: [{"model_ref": ref, "effort": effort}]
            for tier, effort in (("easy", "low"), ("medium", "medium"),
                                 ("hard", "high"))}
        cfg = self.load()
        self.assertEqual([cfg.complexity_profiles[tier][0].effort
                          for tier in ("easy", "medium", "hard")],
                         ["low", "medium", "high"])

    def test_go_max_is_valid_only_when_declared(self):
        self.raw["models"] = [{"provider_id": "opencode-go", "model_id": "kimi-k3",
                               "credential_env": "OPENCODE_GO_API_KEY",
                               "supported_efforts": ["max"]}]
        ref = "opencode-go/kimi-k3"
        self.raw["complexity_profiles"] = {
            tier: [{"model_ref": ref, "effort": "max"}]
            for tier in ("easy", "medium", "hard")}
        self.assertEqual(self.load().complexity_profiles["hard"][0].effort, "max")
        self.raw["complexity_profiles"]["hard"][0]["effort"] = "high"
        with self.assertRaisesRegex(ValueError, "not declared"):
            self.load()

    def test_invalid_complexity_profiles_rejected(self):
        ref = "kimi-code/k3-256k"
        valid = {tier: [{"model_ref": ref, "effort": "low"}]
                 for tier in ("easy", "medium", "hard")}
        for profiles in ({"easy": valid["easy"]},
                         {**valid, "easy": []},
                         {**valid, "easy": [{"model_ref": ref, "effort": "max"}]},
                         {**valid, "easy": [{"model_ref": "missing/model", "effort": "low"}]},
                         {**valid, "easy": [{"model_ref": ref, "effort": "low",
                                              "extra": "unsafe"}]}):
            with self.subTest(profiles=profiles), self.assertRaises(ValueError):
                self.raw["complexity_profiles"] = profiles
                self.load()
        self.raw["complexity_profiles"] = valid
        self.raw["models"][0]["supported_efforts"] = ["medium"]
        with self.assertRaisesRegex(ValueError, "not declared"):
            self.load()

    def test_route_resolves_configured_provider(self):
        self.assertEqual(self.load({"route:priority": "kimi-code"}).routes,
                         {"route:priority": "kimi-code"})

    def test_invalid_routes_rejected(self):
        for routes in ({"route:auto": "kimi-code"},
                       {"route:priority": "missing"},
                       {"priority": "kimi-code"},
                       {"route:Bad": "kimi-code"},
                       ["route:priority"]):
            with self.subTest(routes=routes), self.assertRaises(ValueError):
                self.load(routes)

    def test_bounded_provider_policy_and_quality_source(self):
        self.raw["provider_policies"] = {"kimi-code": {
            "estimated_budget_ceiling": 5.0,
            "attempt_reservation": 0.25,
            "cooldown_seconds": 120}}
        self.raw["quality_metrics_file"] = str(self.path.parent / "quality.json")
        self.raw["quality_min_samples"] = 20
        self.raw["quality_max_age_seconds"] = 3600
        cfg = self.load()
        self.assertEqual(cfg.provider_policies["kimi-code"].attempt_reservation, 0.25)
        self.assertEqual(cfg.provider_policies["kimi-code"].cooldown_seconds, 120)
        self.assertEqual(cfg.quality_min_samples, 20)

    def test_unbounded_policy_and_unknown_provider_rejected(self):
        for policy in ({"missing": {"estimated_budget_ceiling": 1,
                                     "attempt_reservation": 0.1}},
                       {"kimi-code": {"estimated_budget_ceiling": 1,
                                      "attempt_reservation": 2}},
                       {"kimi-code": {"estimated_budget_ceiling": float("nan"),
                                      "attempt_reservation": 0.1}},
                       {"kimi-code": {"estimated_budget_ceiling": 1,
                                      "attempt_reservation": 0.1,
                                      "cooldown_seconds": 1}}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.raw["provider_policies"] = policy
                self.load()
        self.raw.pop("provider_policies")
        self.raw["quality_min_samples"] = 2
        with self.assertRaisesRegex(ValueError, "require quality_metrics_file"):
            self.load()

    def test_optional_local_quota_source(self):
        self.raw["quota_sources"] = {"kimi-code": {
            "kind": "kimi-code", "port": 58627,
            "auth_env": "KIMI_LOCAL_SERVER_TOKEN"}}
        source = self.load().quota_sources["kimi-code"]
        self.assertEqual((source.kind, source.port, source.auth_env),
                         ("kimi-code", 58627, "KIMI_LOCAL_SERVER_TOKEN"))

    def test_invalid_quota_source_rejected(self):
        for sources in ({"unknown": {"kind": "kimi-code", "port": 58627,
                                      "auth_env": "LOCAL_TOKEN"}},
                        {"kimi-code": {"kind": "kimi-code", "port": 0,
                                       "auth_env": "LOCAL_TOKEN"}},
                        {"kimi-code": {"kind": "kimi-code", "port": 58627}},
                        {"kimi-code": {"kind": "opencode-go", "port": 58627,
                                       "auth_env": "GITHUB_TOKEN"}},
                        {"kimi-code": {"kind": "kimi-code", "port": 58627,
                                       "auth_env": "WORKER_SECRET"}}):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                self.raw["quota_sources"] = sources
                self.raw["worker_env"] = ["WORKER_SECRET"]
                self.load()
        self.raw["worker_env"] = []
        self.raw["publish_repo"] = "owner/fork"
        self.raw["publish_token_env"] = "FORK_SECRET"
        self.raw["quota_sources"] = {"kimi-code": {"kind": "kimi-code",
            "port": 58627, "auth_env": "FORK_SECRET"}}
        with self.assertRaises(ValueError):
            self.load()
        self.raw.pop("publish_repo")
        self.raw.pop("publish_token_env")
        self.raw["quota_sources"]["kimi-code"]["auth_env"] = "KIMI_CODE_API_KEY"
        with self.assertRaises(ValueError):
            self.load()


if __name__ == "__main__":
    unittest.main()
