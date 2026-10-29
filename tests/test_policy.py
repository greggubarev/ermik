import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ermik.config import load_config
from ermik.policy import body_sha256, parse_issue, validate_changed_paths


BODY = """## Allowed paths
src/feature
docs/readme.md

## Goal
Add the feature.

## Acceptance
The feature passes its check.
"""


class PolicyTests(unittest.TestCase):
    def test_parse_issue_and_exact_body_digest(self):
        spec = parse_issue(BODY)
        self.assertEqual(spec.allowed_paths, ("src/feature", "docs/readme.md"))
        self.assertEqual(spec.goal, "Add the feature.")
        self.assertEqual(spec.acceptance, "The feature passes its check.")
        self.assertEqual(spec.body_sha256, hashlib.sha256(BODY.encode()).hexdigest())
        self.assertNotEqual(body_sha256(BODY), body_sha256(BODY + "\n"))

    def test_changed_paths_allow_exact_and_descendant_only(self):
        validate_changed_paths(
            ["src/feature", "src/feature/file.py", "docs/readme.md"],
            ("src/feature", "docs/readme.md"),
        )
        for path in ("src/feature-other/file.py", "docs/other.md", "other/file.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_changed_paths([path], ("src/feature", "docs/readme.md"))

    def test_required_sections_and_nonempty_content(self):
        for body in (
            BODY.replace("## Goal", "## Objective"),
            BODY.replace("Add the feature.", ""),
            BODY.replace("The feature passes its check.", ""),
            BODY.replace("src/feature\ndocs/readme.md", ""),
            BODY + "\n## Goal\nRepeated.\n",
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_issue(body)

    def test_unsafe_paths_rejected_in_issue_and_changes(self):
        unsafe = ("", "/absolute", "../outside", "src/../outside", "src//file",
                  "src/./file", ".git/config", "src/.git/config", "src/*.py",
                  "src/file?.py", "C:/root/file", "src\\file", "src/file/",
                  "src/.GIT/config", "src/file:stream")
        for path in unsafe:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_changed_paths([path], ("src",))
            if path:
                with self.subTest(issue_path=path), self.assertRaises(ValueError):
                    parse_issue(BODY.replace("src/feature", path))

    def test_empty_change_set_is_valid_but_empty_scope_is_not(self):
        validate_changed_paths([], ("src",))
        with self.assertRaises(ValueError):
            validate_changed_paths(["src/file.py"], ())

    def test_publish_repo_is_optional_and_requires_a_distinct_repo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {"repo": "example/project", "maintainer": "example",
                   "state_dir": str(root / "state"), "workspace": str(root / "work"),
                   "image": "example:latest", "worker_command": ["true"],
                   "checks": [["true"]], "max_parallel": 1, "poll_seconds": 10}
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertIsNone(load_config(path).publish_repo)
            path.write_text(json.dumps({**raw, "publish_repo": "publisher/project"}),
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "publish_token_env"):
                load_config(path)
            fork_config = {**raw, "publish_repo": "publisher/project",
                           "publish_token_env": "ERMIK_FORK_TOKEN"}
            path.write_text(json.dumps(fork_config), encoding="utf-8")
            self.assertEqual(load_config(path).publish_repo, "publisher/project")
            self.assertEqual(load_config(path).publish_token_env, "ERMIK_FORK_TOKEN")
            for invalid in ("example/project", "Example/Project", "../project", "", 1):
                with self.subTest(invalid=invalid):
                    path.write_text(json.dumps({**fork_config, "publish_repo": invalid}),
                                    encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "publish_repo"):
                        load_config(path)
            for invalid in ("GH_TOKEN", "GITHUB_TOKEN", "", "BAD-NAME", 1):
                with self.subTest(invalid_token_env=invalid):
                    path.write_text(json.dumps({**fork_config,
                                                "publish_token_env": invalid}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "publish_token_env"):
                        load_config(path)
            broker_config = {**fork_config, "draft_pr_socket": str(root / "broker.sock"),
                             "draft_pr_evidence_dir": str(root / "proof")}
            path.write_text(json.dumps(broker_config), encoding="utf-8")
            self.assertEqual(load_config(path).draft_pr_evidence_dir, root / "proof")
            for invalid in (str(root / "state" / "proof"), "relative-proof", 3):
                with self.subTest(invalid_evidence_dir=invalid):
                    path.write_text(json.dumps({**broker_config,
                                                "draft_pr_evidence_dir": invalid}),
                                    encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "draft_pr_evidence_dir"):
                        load_config(path)


if __name__ == "__main__":
    unittest.main()
