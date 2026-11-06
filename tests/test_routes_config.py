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
                        "credential_env": "KIMI_CODE_API_KEY"}],
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


if __name__ == "__main__":
    unittest.main()
