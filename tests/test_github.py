"""Tests for the GitHub PR identity boundary."""

import json
import io
import os
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ermik.github import GitHub, GitHubError


HEAD = "a" * 40
BASE = "b" * 40
BRANCH = "work/issue-42-delivery"


def pr_row(number=57, *, repo="example/project", branch=BRANCH, base="main",
           head=HEAD, draft=True, state="OPEN"):
    return {"number": number, "headRepository": {"nameWithOwner": repo},
            "headRefName": branch, "baseRefName": base, "headRefOid": head,
            "baseRefOid": BASE, "isDraft": draft, "state": state}


def rest_pr(number=57, *, repo="example/project", branch=BRANCH, base="main"):
    return {"number": number, "head": {"ref": branch, "repo": {"full_name": repo}},
            "base": {"ref": base}}


class GitHubPRIdentityTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub("example/project")

    def test_find_branch_pr_matches_source_repository_branch_and_base(self):
        rows = [rest_pr(1, repo="other/project"),
                rest_pr(2, branch="other-branch"),
                rest_pr(3, base="release"),
                rest_pr(57, repo="Example/Project")]
        with patch.object(self.github, "_pages", return_value=rows) as pages:
            self.assertEqual(self.github.find_branch_pr(BRANCH, "main"), 57)
        self.assertIn("head=example%3A", pages.call_args.args[0])

    def test_find_branch_pr_ignores_foreign_or_unknown_source(self):
        rows = [rest_pr(1, repo="other/project"),
                {**rest_pr(2), "head": {"ref": BRANCH, "repo": None}}]
        with patch.object(self.github, "_pages", return_value=rows):
            self.assertIsNone(self.github.find_branch_pr(BRANCH, "main"))

    def test_find_branch_pr_rejects_multiple_matching_prs(self):
        rows = [rest_pr(1), rest_pr(2), rest_pr(3, repo="other/project")]
        with patch.object(self.github, "_pages", return_value=rows):
            with self.assertRaisesRegex(GitHubError, "multiple PRs"):
                self.github.find_branch_pr(BRANCH, "main")

    def test_validate_pr_identity_accepts_exact_pr(self):
        self.github.validate_pr_identity(pr_row(), number=57, branch=BRANCH,
                                         base_branch="main", head_sha=HEAD,
                                         base_sha=BASE, draft=True, state="OPEN")

    def test_validate_pr_identity_rejects_each_mismatch(self):
        mutations = ({"headRepository": {"nameWithOwner": "other/project"}},
                     {"headRepository": None}, {"number": 58},
                     {"headRefName": "other-branch"}, {"baseRefName": "release"},
                     {"headRefOid": "c" * 40}, {"baseRefOid": "d" * 40},
                     {"isDraft": False}, {"state": "MERGED"})
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(GitHubError, "PR identity"):
                    self.github.validate_pr_identity({**pr_row(), **mutation},
                                                     number=57, branch=BRANCH,
                                                     base_branch="main", head_sha=HEAD,
                                                     base_sha=BASE, draft=True,
                                                     state="OPEN")

    def test_pr_normalizes_portable_rest_identity_and_files(self):
        raw = {"number": 57, "state": "open", "draft": True,
               "head": {"ref": BRANCH, "sha": HEAD,
                        "repo": {"full_name": "example/project"}},
               "base": {"ref": "main", "sha": BASE}, "body": "Closes #42",
               "changed_files": 1, "merge_commit_sha": None, "merged_at": None}
        with patch.object(self.github, "api", return_value=raw), patch.object(
                self.github, "_pages", return_value=[{"filename": "src/example.py"}]):
            result = self.github.pr(57)
        self.assertEqual(result["headRepository"], {"nameWithOwner": "example/project"})
        self.assertEqual(result["baseRefOid"], BASE)
        self.assertEqual(result["files"], [{"path": "src/example.py"}])
        self.github.validate_pr_identity(result, number=57, branch=BRANCH,
                                         base_branch="main", head_sha=HEAD,
                                         base_sha=BASE, draft=True, state="OPEN")

    def test_fork_pr_uses_fork_owner_and_verifies_fork_source(self):
        github = GitHub("example/project", "publisher/project", "ERMIK_FORK_TOKEN")
        rows = [rest_pr(11, repo="example/project"),
                rest_pr(57, repo="publisher/project")]
        with patch.object(github, "_pages", return_value=rows) as pages:
            self.assertEqual(github.find_branch_pr(BRANCH, "main"), 57)
        self.assertIn("head=publisher%3A", pages.call_args.args[0])
        github.validate_pr_identity(pr_row(repo="publisher/project"),
                                    number=57, branch=BRANCH, base_branch="main")
        with self.assertRaises(GitHubError):
            github.validate_pr_identity(pr_row(), branch=BRANCH, base_branch="main")
        with patch.object(github, "command", return_value="https://github.com/example/project/pull/57\n") as command:
            self.assertEqual(github.open_pr(BRANCH, "body", "title", "main"), 57)
        self.assertEqual(command.call_args.args[0][6:8], ["--head", f"publisher:{BRANCH}"])

    def test_fork_access_requires_exact_parent_and_probes_both_credentials(self):
        github = GitHub("example/project", "publisher/project", "ERMIK_FORK_TOKEN")
        upstream = {"full_name": "example/project", "default_branch": "main",
                    "permissions": {"push": True}}
        fork = {"full_name": "publisher/project", "fork": True,
                "parent": {"full_name": "example/project"}, "default_branch": "main"}
        reference = lambda repo: json.dumps({"ref": "refs/heads/main", "object": {"sha": HEAD}})
        responses = [json.dumps(upstream), json.dumps(fork), reference("example/project"),
                     reference("publisher/project")]
        with patch.dict(os.environ, {"GH_TOKEN": "upstream-test", "ERMIK_FORK_TOKEN": "fork-test"}), patch.object(
                github, "command", side_effect=responses) as command, patch.object(
                    github, "_fork_token_upstream_hidden", return_value=True), patch.object(
                    github, "_probe_branch_write", side_effect=[False, False, True]) as probe, patch.object(
                    github, "_probe_existing_branch_update", return_value=False) as update_probe:
            github.validate_publish_access()
        self.assertNotIn("credential_env", command.call_args_list[0].kwargs)
        self.assertEqual(command.call_args_list[1].kwargs["credential_env"],
                         "ERMIK_FORK_TOKEN")
        self.assertEqual([call.args[0] for call in probe.call_args_list],
                         ["example/project", "example/project", "publisher/project"])
        self.assertEqual([call.args[0:2] for call in update_probe.call_args_list],
                         [("example/project", "main"), ("example/project", "main")])
        for wrong_upstream, wrong_fork in (
                ({**upstream, "full_name": "other/project"}, fork),
                (upstream, {**fork, "full_name": "other/project"}),
                (upstream, {**fork, "parent": {"full_name": "other/project"}}),
                (upstream, {**fork, "fork": False})):
            with self.subTest(wrong_fork=wrong_fork, wrong_upstream=wrong_upstream):
                with patch.object(github, "command", side_effect=[json.dumps(wrong_upstream),
                                                                  json.dumps(wrong_fork)]):
                    with self.assertRaisesRegex(GitHubError, "fork identity"):
                        github.validate_publish_access()

    def test_publication_probe_cleans_created_branch_and_rejects_ambiguous_status(self):
        github = GitHub("example/project", "publisher/project", "ERMIK_FORK_TOKEN")

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        def response(status):
            value = Response(b"{}")
            value.status = status
            return value

        def denied(status):
            return HTTPError("https://api.github.com", status, "denied", None, None)

        with patch("ermik.github.urlopen", side_effect=denied(403)) as request:
            self.assertFalse(github._probe_branch_write("example/project", HEAD, "secret"))
            self.assertEqual(request.call_count, 1)
        with patch("ermik.github.urlopen", side_effect=[response(201), response(204)]) as request:
            self.assertTrue(github._probe_branch_write("publisher/project", HEAD, "secret"))
            self.assertEqual([call.args[0].get_method() for call in request.call_args_list],
                             ["POST", "DELETE"])
            self.assertIn("/publisher/project/git/refs/heads/ermik-access-probe-",
                          request.call_args_list[1].args[0].full_url)
        with patch("ermik.github.urlopen", side_effect=[response(201), denied(403)]) as request:
            with self.assertRaisesRegex(GitHubError, "cleanup"):
                github._probe_branch_write("example/project", HEAD, "secret")
            self.assertEqual(request.call_count, 2)
        for status in (404, 422):
            with self.subTest(status=status), patch("ermik.github.urlopen", side_effect=denied(status)):
                with self.assertRaisesRegex(GitHubError, "inconclusive"):
                    github._probe_branch_write("example/project", HEAD, "secret")
        with patch("ermik.github.urlopen", side_effect=denied(404)):
            self.assertFalse(github._probe_branch_write("example/project", HEAD, "secret",
                                                        allow_404=True))

    def test_existing_branch_update_probe_distinguishes_permission_from_validation(self):
        github = GitHub("example/project", "publisher/project", "ERMIK_FORK_TOKEN")

        def denied(status):
            return HTTPError("https://api.github.com", status, "secret response", None, None)

        with patch("ermik.github.urlopen", side_effect=denied(403)) as request:
            self.assertFalse(github._probe_existing_branch_update("example/project", "main", "secret"))
            sent = request.call_args.args[0]
            self.assertEqual(sent.get_method(), "PATCH")
            self.assertEqual(sent.full_url,
                             "https://api.github.com/repos/example/project/git/refs/heads/main")
            self.assertEqual(json.loads(sent.data), {"sha": "0" * 40, "force": False})
            self.assertEqual(sent.get_header("Content-type"), "application/json")
        with patch("ermik.github.urlopen", side_effect=denied(422)):
            self.assertTrue(github._probe_existing_branch_update("example/project", "main", "secret"))
        with patch("ermik.github.urlopen", side_effect=denied(404)):
            self.assertFalse(github._probe_existing_branch_update("example/project", "main", "secret",
                                                                   allow_404=True))
            with self.assertRaisesRegex(GitHubError, "inconclusive"):
                github._probe_existing_branch_update("example/project", "main", "secret")
        with patch("ermik.github.urlopen", side_effect=denied(429)):
            with self.assertRaisesRegex(GitHubError, "inconclusive") as failure:
                github._probe_existing_branch_update("example/project", "main", "secret")
            self.assertNotIn("secret response", str(failure.exception))
        with patch("ermik.github.urlopen", return_value=io.BytesIO(b"{}")):
            with self.assertRaisesRegex(GitHubError, "inconclusive"):
                github._probe_existing_branch_update("example/project", "main", "secret")

    def test_publication_access_rejects_unsafe_upstream_and_unwritable_fork(self):
        github = GitHub("example/project", "publisher/project", "ERMIK_FORK_TOKEN")
        upstream = {"full_name": "example/project", "default_branch": "main"}
        fork = {"full_name": "publisher/project", "fork": True,
                "parent": {"full_name": "example/project"}, "default_branch": "main"}
        ref = {"ref": "refs/heads/main", "object": {"sha": HEAD}}
        responses = list(map(json.dumps, (upstream, fork, ref, ref)))
        with patch.dict(os.environ, {"GH_TOKEN": "upstream-test", "ERMIK_FORK_TOKEN": "fork-test"}):
            with patch.object(github, "command", side_effect=responses), patch.object(
                    github, "_probe_branch_write", return_value=True) as probe:
                with self.assertRaisesRegex(GitHubError, "upstream credential can write"):
                    github.validate_publish_access()
                self.assertEqual(probe.call_count, 1)
            with patch.object(github, "command", side_effect=responses), patch.object(
                    github, "_fork_token_upstream_hidden", return_value=True), patch.object(
                    github, "_probe_branch_write", side_effect=[False, True]) as probe, patch.object(
                    github, "_probe_existing_branch_update", return_value=False):
                with self.assertRaisesRegex(GitHubError, "fork credential can write the upstream"):
                    github.validate_publish_access()
                self.assertEqual(probe.call_count, 2)
            with patch.object(github, "command", side_effect=responses), patch.object(
                    github, "_fork_token_upstream_hidden", return_value=True), patch.object(
                    github, "_probe_branch_write", side_effect=[False, False, False]) as probe, patch.object(
                    github, "_probe_existing_branch_update", return_value=False):
                with self.assertRaisesRegex(GitHubError, "cannot write the publication"):
                    github.validate_publish_access()
                self.assertEqual(probe.call_count, 3)

    def test_publication_access_rejects_upstream_update_capability_after_creation_refusal(self):
        github = GitHub("example/project", "publisher/project", "ERMIK_FORK_TOKEN")
        upstream = {"full_name": "example/project", "default_branch": "main"}
        fork = {"full_name": "publisher/project", "fork": True,
                "parent": {"full_name": "example/project"}, "default_branch": "main"}
        ref = {"ref": "refs/heads/main", "object": {"sha": HEAD}}
        responses = list(map(json.dumps, (upstream, fork, ref, ref)))
        with patch.dict(os.environ, {"GH_TOKEN": "upstream-test", "ERMIK_FORK_TOKEN": "fork-test"}), patch.object(
                github, "command", side_effect=responses), patch.object(
                github, "_probe_branch_write", return_value=False), patch.object(
                github, "_probe_existing_branch_update", return_value=True):
            with self.assertRaisesRegex(GitHubError, "upstream credential can write"):
                github.validate_publish_access()
        with patch.dict(os.environ, {"GH_TOKEN": "upstream-test", "ERMIK_FORK_TOKEN": "fork-test"}), patch.object(
                github, "command", side_effect=responses), patch.object(
                github, "_fork_token_upstream_hidden", return_value=True), patch.object(
                github, "_probe_branch_write", side_effect=[False, False]), patch.object(
                github, "_probe_existing_branch_update", side_effect=[False, True]):
            with self.assertRaisesRegex(GitHubError, "fork credential can write the upstream"):
                github.validate_publish_access()

    def test_fork_upstream_visibility_requires_exact_identity_or_404(self):
        github = GitHub("example/project", "publisher/project", "ERMIK_FORK_TOKEN")
        class Response(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                self.close()
        with patch("ermik.github.urlopen", return_value=Response(b'{"full_name":"example/project"}')):
            self.assertFalse(github._fork_token_upstream_hidden("secret"))
        with patch("ermik.github.urlopen", return_value=Response(b'{"full_name":"other/project"}')):
            with self.assertRaisesRegex(GitHubError, "identity"):
                github._fork_token_upstream_hidden("secret")
        with patch("ermik.github.urlopen", side_effect=HTTPError("url", 404, "hidden", None, None)):
            self.assertTrue(github._fork_token_upstream_hidden("secret"))

    def test_fork_command_passes_token_only_in_child_environment(self):
        github = GitHub("example/project", "publisher/project", "ERMIK_FORK_TOKEN")
        completed = subprocess.CompletedProcess(["gh", "api", "user"], 0, stdout="{}")
        with patch.dict(os.environ, {"GH_TOKEN": "upstream-secret",
                                  "ERMIK_FORK_TOKEN": "fork-secret"}), patch(
                "ermik.github.subprocess.run", return_value=completed) as run:
            self.assertEqual(github.command(["api", "user"],
                                            credential_env="ERMIK_FORK_TOKEN"), "{}")
        kwargs = run.call_args.kwargs
        self.assertEqual(run.call_args.args[0], ["gh", "api", "user"])
        self.assertEqual(kwargs["env"]["GH_TOKEN"], "fork-secret")
        self.assertNotIn("fork-secret", str(run.call_args.args[0]))
        with patch.dict(os.environ, {"GH_TOKEN": "same-secret",
                                  "ERMIK_FORK_TOKEN": "same-secret"}):
            with self.assertRaisesRegex(GitHubError, "must differ"):
                github.command(["api", "user"], credential_env="ERMIK_FORK_TOKEN")
        with patch.dict(os.environ, {"ERMIK_FORK_TOKEN": ""}):
            with self.assertRaisesRegex(GitHubError, "unavailable"):
                github.command(["api", "user"], credential_env="ERMIK_FORK_TOKEN")


if __name__ == "__main__":
    unittest.main()
