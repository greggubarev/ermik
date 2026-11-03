"""Bounded retries for read-only GitHub CLI requests."""

import subprocess
import unittest
from unittest.mock import patch

from ermik.github import GitHub, GitHubError


class GitHubReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub("example/project")
        self.read = ["api", "repos/example/project/issues/42"]

    @staticmethod
    def completed(status=0, stdout='{"number":42}', stderr=""):
        return subprocess.CompletedProcess(["gh", "api"], status,
                                           stdout=stdout, stderr=stderr)

    def test_transient_http_status_retries_then_returns_read_result(self):
        for status in (502, 503, 504):
            with self.subTest(status=status), patch(
                    "ermik.github.subprocess.run",
                    side_effect=[self.completed(1, stderr=f"gh: HTTP {status}: temporary failure"),
                                 self.completed()]) as run, patch(
                    "ermik.github.time.sleep") as sleep:
                self.assertEqual(self.github.command(self.read), '{"number":42}')
                self.assertEqual(run.call_count, 2)
                sleep.assert_called_once()

    def test_transient_http_status_has_three_total_attempts(self):
        failure = self.completed(1, stderr="gh: HTTP 503: Service Unavailable")
        with patch("ermik.github.subprocess.run", return_value=failure) as run, patch(
                "ermik.github.time.sleep") as sleep:
            with self.assertRaises(GitHubError):
                self.github.command(self.read)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_transport_failures_retry_but_remain_bounded(self):
        errors = (OSError("connection reset"),
                  subprocess.TimeoutExpired(["gh", "api"], 1))
        for error in errors:
            with self.subTest(error=type(error).__name__), patch(
                    "ermik.github.subprocess.run",
                    side_effect=[error, self.completed()]) as run, patch(
                    "ermik.github.time.sleep") as sleep:
                self.assertEqual(self.github.command(self.read), '{"number":42}')
                self.assertEqual(run.call_count, 2)
                sleep.assert_called_once()
            with self.subTest(error=type(error).__name__, exhausted=True), patch(
                    "ermik.github.subprocess.run", side_effect=error) as run, patch(
                    "ermik.github.time.sleep") as sleep:
                with self.assertRaises(GitHubError):
                    self.github.command(self.read)
                self.assertEqual(run.call_count, 3)
                self.assertEqual(sleep.call_count, 2)

    def test_rate_limit_and_permanent_http_status_do_not_retry_immediately(self):
        for status in (429, 403, 404):
            with self.subTest(status=status), patch(
                    "ermik.github.subprocess.run",
                    return_value=self.completed(1, stderr=f"gh: HTTP {status}: denied")) as run, patch(
                    "ermik.github.time.sleep") as sleep:
                with self.assertRaises(GitHubError):
                    self.github.command(self.read)
                run.assert_called_once()
                sleep.assert_not_called()

    def test_http_status_overrides_transport_phrase(self):
        for status in (429, 403, 501):
            with self.subTest(status=status), patch(
                    "ermik.github.subprocess.run",
                    return_value=self.completed(1, stderr=f"gh: HTTP {status}: connection reset")) as run, patch(
                    "ermik.github.time.sleep") as sleep:
                with self.assertRaises(GitHubError):
                    self.github.command(self.read)
                run.assert_called_once()
                sleep.assert_not_called()

    def test_mutating_commands_never_retry_transient_http_error(self):
        commands = (
            ["api", "--method", "POST", "repos/example/project/issues/42/comments"],
            ["api", "--method", "PATCH", "repos/example/project/issues/42"],
            ["api", "--method", "DELETE", "repos/example/project/issues/42"],
            ["api", "graphql", "-f", "query={viewer{login}}"],
            ["api", "repos/example/project/issues/42/comments", "-f", "body=example"],
            ["pr", "merge", "42", "-R", "example/project"],
            ["pr", "ready", "42", "-R", "example/project"],
            ["issue", "edit", "42", "-R", "example/project", "--add-label", "ready"],
        )
        for args in commands:
            with self.subTest(args=args), patch(
                    "ermik.github.subprocess.run",
                    return_value=self.completed(1, stderr="gh: HTTP 503: Service Unavailable")) as run, patch(
                    "ermik.github.time.sleep") as sleep:
                with self.assertRaises(GitHubError):
                    self.github.command(args)
                run.assert_called_once()
                sleep.assert_not_called()

    def test_mutating_commands_never_retry_transport_failure(self):
        commands = (["api", "--method", "POST", "repos/example/project/issues/42/comments"],
                    ["pr", "merge", "42", "-R", "example/project"],
                    ["issue", "edit", "42", "-R", "example/project"])
        for args in commands:
            with self.subTest(args=args), patch(
                    "ermik.github.subprocess.run", side_effect=OSError("connection reset")) as run, patch(
                    "ermik.github.time.sleep") as sleep:
                with self.assertRaises(GitHubError):
                    self.github.command(args)
                run.assert_called_once()
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
