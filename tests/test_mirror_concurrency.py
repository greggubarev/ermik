"""Shared Git metadata stays serial when two issue slots start together."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from ermik import runtime


class MirrorConcurrencyTests(unittest.TestCase):
    def test_concurrent_checkouts_serialize_shared_git_operations(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def checkout(_cfg, issue, branch, *, correction):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with guard:
                active -= 1
            return Path(f"/work/issue-{issue}")

        with patch.object(runtime, "_checkout_locked", side_effect=checkout):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda n: runtime._checkout(
                    None, n, f"work/issue-{n}-delivery", correction=False), (1, 2)))
        self.assertEqual(results, [Path("/work/issue-1"), Path("/work/issue-2")])
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
