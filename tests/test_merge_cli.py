"""The command line must not report an incomplete merge as success."""

from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import patch

from ermik import __main__ as cli


class MergeCliTests(unittest.TestCase):
    def test_incomplete_merge_exits_nonzero_with_result(self):
        output = StringIO()
        result = {"pr": 19, "status": "incomplete",
                  "reason": "post-merge checks failed"}
        with patch.object(cli, "load_config", return_value=object()), patch.object(
                cli, "merge", return_value=result), redirect_stdout(output):
            code = cli.main(["--config", "unused.json", "merge", "19"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), result)

    def test_complete_merge_exits_zero(self):
        output = StringIO()
        result = {"pr": 19, "status": "complete", "merge_commit": "a" * 40}
        with patch.object(cli, "load_config", return_value=object()), patch.object(
                cli, "merge", return_value=result), redirect_stdout(output):
            code = cli.main(["--config", "unused.json", "merge", "19"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), result)


if __name__ == "__main__":
    unittest.main()
