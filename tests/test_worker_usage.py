"""Usage accounting from an isolated OpenCode session export."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ermik.providers import ModelCandidate


WORKER = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                            "examples" / "opencode-worker.py"))
SESSION = "ses_1234abcd"


def _export():
    return {"info": {"id": SESSION, "title": "private prompt"}, "messages": [
        {"info": {"role": "user", "content": "private prompt"}, "parts": []},
        {"info": {"role": "assistant", "tokens": {
            "input": 10, "output": 3, "reasoning": 1, "total": 18,
            "cache": {"read": 4, "write": 0}}, "cost": 0.01},
         "parts": [{"type": "text", "text": "private answer"}]},
        {"info": {"role": "assistant", "tokens": {
            "input": 20, "output": 5, "reasoning": 2, "total": 34,
            "cache": {"read": 7, "write": 0}}, "cost": 0.02},
         "parts": [{"type": "step-finish", "cost": 0.02}]},
    ]}


class WorkerUsageTests(unittest.TestCase):
    def test_export_sums_assistant_messages_once(self):
        usage = WORKER["_parse_usage"](_export(), SESSION)
        self.assertEqual(usage, {"input": 30, "output": 8, "reasoning": 3,
                                 "cache_read": 11, "cache_write": 0,
                                 "total": 52, "estimated_cost": 0.03,
                                 "complete": True})

    def test_isolated_list_and_export_with_bounded_commands(self):
        calls = []

        def cli(command, environment):
            calls.append(command)
            return json.dumps([{"id": SESSION}] if command[1] == "session"
                              else _export()).encode()

        with patch.dict(WORKER["_session_usage"].__globals__, {"_bounded_cli": cli}):
            self.assertTrue(WORKER["_session_usage"]({})["complete"])
        self.assertEqual(calls, [
            ["opencode", "session", "list", "--max-count", "2", "--format", "json"],
            ["opencode", "export", SESSION]])

    def test_unavailable_or_ambiguous_usage_has_only_nulls(self):
        for listing in ([], [{"id": SESSION}, {"id": "ses_other"}],
                        [{"id": "ses_bad;echo secret"}]):
            with self.subTest(listing=listing):
                with patch.dict(WORKER["_session_usage"].__globals__, {
                    "_bounded_cli": lambda *_: json.dumps(listing).encode()}):
                    usage = WORKER["_session_usage"]({})
                self.assertFalse(usage.pop("complete"))
                self.assertTrue(all(value is None for value in usage.values()))
        malformed = _export()
        del malformed["messages"][1]["info"]["tokens"]["total"]
        with self.assertRaises(ValueError):
            WORKER["_parse_usage"](malformed, SESSION)

    def test_printed_usage_excludes_export_text_and_credentials(self):
        secret = "provider-secret-value"
        exported = _export()
        exported["messages"][1]["parts"][0]["text"] = secret

        def cli(command, environment):
            return json.dumps([{"id": SESSION}] if command[1] == "session"
                              else exported).encode()

        output = StringIO()
        with patch.dict(WORKER["_print_usage"].__globals__, {"_bounded_cli": cli}), \
                redirect_stdout(output):
            WORKER["_print_usage"]({"PROVIDER_API_KEY": secret})
        line = output.getvalue().strip()
        self.assertEqual(line.count("\n"), 0)
        self.assertTrue(line.startswith("WORKER_USAGE {"))
        self.assertNotIn(secret, line)
        self.assertNotIn("private", line)
        self.assertTrue(json.loads(line.removeprefix("WORKER_USAGE "))["complete"])

    def test_build_agent_has_step_cap_and_provider_key_stays_scoped(self):
        candidate = ModelCandidate("opencode-go", "kimi-k3", "OPENCODE_GO_API_KEY")
        with patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "secret"}):
            config = WORKER["_config"](candidate)
        self.assertEqual(config["agent"]["build"]["steps"], 24)
        self.assertEqual(config["provider"]["opencode-go"],
                         {"options": {"apiKey": "secret"}})

    def test_export_command_rejects_oversize_output_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            globals_ = WORKER["_bounded_cli"].__globals__
            with patch.dict(globals_, {"WORK": Path(directory), "EXPORT_LIMIT": 256}):
                with self.assertRaises(ValueError):
                    WORKER["_bounded_cli"](
                        [sys.executable, "-c", "print('x' * 10000)"], {})
            with patch.dict(globals_, {"WORK": Path(directory), "EXPORT_TIMEOUT": 0.05}):
                with self.assertRaises(ValueError):
                    WORKER["_bounded_cli"](
                        [sys.executable, "-c", "import time; time.sleep(2)"], {})

    def test_failed_run_emits_usage_without_provider_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "issue.md"
            task.write_text("Do the task", encoding="utf-8")
            output = StringIO()
            globals_ = WORKER["main"].__globals__
            with patch.dict(globals_, {"TASK": task, "WORK": Path(directory),
                                       "_session_usage": lambda _: WORKER["_unavailable_usage"]()}), \
                    patch.dict("os.environ", {"ERMIK_MODEL_REF": "opencode-go/kimi-k3",
                                             "OPENCODE_GO_API_KEY": "secret"}), \
                    patch.object(WORKER["subprocess"], "run",
                                 return_value=subprocess.CompletedProcess([], 1, "", "secret-error")), \
                    redirect_stdout(output):
                self.assertEqual(WORKER["main"](), 1)
            self.assertEqual(len(output.getvalue().splitlines()), 2)
            self.assertNotIn("secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
