"""The host gate must refuse incomplete verification evidence."""

import unittest
from unittest.mock import patch

from ermik import host_verify


class HostVerifyTests(unittest.TestCase):
    def test_unit_only_records_docker_as_not_requested(self):
        with patch.object(host_verify, "_probe", side_effect=[str(host_verify._ROOT),
                                                               "a" * 40, None,
                                                               "a" * 40]), patch.object(
                host_verify, "_dirty_checkout", side_effect=[False, False]), patch.object(
                    host_verify, "_unit_suite", return_value={"result": "passed"}):
            result = host_verify.verify()
        self.assertEqual(result["result"], "passed")
        self.assertEqual(result["host"], {"result": "not_requested"})

    def test_dirty_checkout_fails_even_when_tests_pass(self):
        with patch.object(host_verify, "_probe", side_effect=[str(host_verify._ROOT),
                                                               "a" * 40, None,
                                                               "a" * 40]), patch.object(
                host_verify, "_dirty_checkout", side_effect=[True, True]), patch.object(
                    host_verify, "_unit_suite", return_value={"result": "passed"}):
            result = host_verify.verify()
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["reason"], "checkout_dirty")

    def test_requested_docker_requires_server_and_image(self):
        with patch.object(host_verify, "_probe", side_effect=[str(host_verify._ROOT),
                                                               "a" * 40, None, None,
                                                               "a" * 40]), patch.object(
                host_verify, "_dirty_checkout", side_effect=[False, False]), patch.object(
                    host_verify, "_unit_suite", return_value={"result": "passed"}):
            result = host_verify.verify(docker=True, image="fixture:latest")
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["host"]["reason"], "docker_or_image_unavailable")


if __name__ == "__main__":
    unittest.main()
