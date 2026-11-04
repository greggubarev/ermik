"""The host gate must refuse incomplete verification evidence."""

import unittest
from unittest.mock import patch

from ermik import host_verify


class HostVerifyTests(unittest.TestCase):
    def test_windows_retention_skips_require_exact_case_and_reason(self):
        expected = {
            "test_retention.RetentionTests.test_world_readable_evidence_in_public_parent_remains":
                "POSIX file modes only",
            "test_retention.RetentionTests.test_wrong_owner_refuses_cleanup":
                "POSIX ownership only",
        }
        with patch.object(host_verify.os, "name", "nt"):
            for case, reason in expected.items():
                with self.subTest(case=case):
                    self.assertTrue(host_verify._allowed_skip(case, reason))
                    self.assertFalse(host_verify._allowed_skip(case, "unexpected environment"))
            self.assertFalse(host_verify._allowed_skip(
                "test_retention.RetentionTests.test_unlisted_case", "POSIX file modes only"))
        with patch.object(host_verify.os, "name", "posix"):
            for case, reason in expected.items():
                self.assertFalse(host_verify._allowed_skip(case, reason))

    def test_empty_unit_suite_fails(self):
        with patch.object(host_verify.unittest.TestLoader, "discover",
                          return_value=unittest.TestSuite()):
            result = host_verify._unit_suite()
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["executed"], 0)

    def test_skipped_only_unit_suite_fails(self):
        class SkippedCase(unittest.TestCase):
            @unittest.skip("unexpected environment")
            def test_case(self):
                pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SkippedCase)
        with patch.object(host_verify.unittest.TestLoader, "discover",
                          return_value=suite):
            result = host_verify._unit_suite()
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["run"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["executed"], 0)

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

    def test_docker_checks_use_recorded_immutable_image_id(self):
        image_id = "sha256:" + "a" * 64
        with patch.object(host_verify, "_probe", side_effect=[str(host_verify._ROOT),
                                                               "b" * 40, "28.2.2",
                                                               image_id, "b" * 40]), patch.object(
                host_verify, "_dirty_checkout", side_effect=[False, False]), patch.object(
                    host_verify, "_unit_suite", return_value={"result": "passed"}), patch.object(
                        host_verify, "_host_check", return_value={"result": "passed"}) as check:
            result = host_verify.verify(docker=True, image="mutable:tag")
        self.assertEqual(result["result"], "passed")
        self.assertEqual([call.args[1] for call in check.call_args_list],
                         [image_id, image_id])


if __name__ == "__main__":
    unittest.main()
