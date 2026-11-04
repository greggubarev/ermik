from pathlib import Path
import tempfile
import unittest

from ermik.ci import verify_junit


class JUnitTests(unittest.TestCase):
    def verify_xml(self, xml: str, *, min_tests: int = 1) -> dict:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "report.xml"
            path.write_text(xml, encoding="utf-8")
            return verify_junit(path, min_tests=min_tests)

    def test_nonempty_passes_and_failure_rejects(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "report.xml"
            path.write_text('<testsuite><testcase name="a"/><testcase name="b"/></testsuite>')
            self.assertEqual(verify_junit(path, min_tests=2)["tests"], 2)
            with self.assertRaisesRegex(ValueError, "required 3"):
                verify_junit(path, min_tests=3)
            path.write_text('<testsuite><testcase name="a"><failure/></testcase></testsuite>')
            with self.assertRaisesRegex(ValueError, "failed"):
                verify_junit(path)

    def test_empty_and_unexpected_xml_reject(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "report.xml"
            path.write_text("<testsuite/>")
            with self.assertRaisesRegex(ValueError, "only 0"):
                verify_junit(path)
            path.write_text("<other><testcase/></other>")
            with self.assertRaisesRegex(ValueError, "root"):
                verify_junit(path)

    def test_minimum_counts_executed_cases_not_skips(self):
        report = (
            '<testsuite tests="3" failures="0" errors="0" skipped="2">'
            '<testcase name="passed"/>'
            '<testcase name="skip-one"><skipped/></testcase>'
            '<testcase name="skip-two"><skipped/></testcase>'
            '</testsuite>'
        )
        result = self.verify_xml(report, min_tests=1)
        self.assertEqual(result["tests"], 3)
        self.assertEqual(result["executed"], 1)
        self.assertEqual(result["skipped"], 2)
        with self.assertRaises(ValueError):
            self.verify_xml(report, min_tests=2)

    def test_skipped_only_is_rejected(self):
        with self.assertRaises(ValueError):
            self.verify_xml(
                '<testsuite tests="2" failures="0" errors="0" skipped="2">'
                '<testcase name="a"><skipped/></testcase>'
                '<testcase name="b"><skipped/></testcase>'
                '</testsuite>'
            )

    def test_failure_and_error_each_reject(self):
        for outcome in ("failure", "error"):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                self.verify_xml(
                    f'<testsuite tests="1" failures="{int(outcome == "failure")}" '
                    f'errors="{int(outcome == "error")}" skipped="0">'
                    f'<testcase name="a"><{outcome}/></testcase>'
                    '</testsuite>'
                )

    def test_missing_malformed_and_partial_reports_reject(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                verify_junit(Path(root) / "missing.xml")
        for xml in (
            '<testsuite><testcase name="a">',
            '<testsuite tests="2" failures="0" errors="0" skipped="0">'
            '<testcase name="a"/></testsuite>',
            '<testsuite tests="1" failures="0"><testcase name="a"/></testsuite>',
            '<testsuite tests="one" failures="0" errors="0" skipped="0">'
            '<testcase name="a"/></testsuite>',
            '<testsuite tests="-1" failures="0" errors="0" skipped="0">'
            '<testcase name="a"/></testsuite>',
        ):
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                self.verify_xml(xml)

    def test_suite_outcome_counters_must_match_cases(self):
        for counters, outcome in (
            ('tests="1" failures="1" errors="0" skipped="0"', ''),
            ('tests="1" failures="0" errors="1" skipped="0"', ''),
            ('tests="1" failures="0" errors="0" skipped="1"', ''),
            ('tests="1" failures="0" errors="0" skipped="0"', '<skipped/>'),
        ):
            with self.subTest(counters=counters, outcome=outcome), self.assertRaises(ValueError):
                self.verify_xml(
                    f'<testsuite {counters}><testcase name="a">{outcome}'
                    '</testcase></testsuite>'
                )

    def test_unexpected_structure_and_mixed_outcomes_reject(self):
        for xml in (
            '<testsuite><unexpected><testcase name="a"/></unexpected></testsuite>',
            '<testsuites><testcase name="a"/></testsuites>',
            '<testsuite><testcase name="a"><unexpected/></testcase></testsuite>',
            '<testsuite><testcase name="a"><skipped/><failure/></testcase></testsuite>',
            '<testsuite><testcase name="a"><skipped/><skipped/></testcase></testsuite>',
        ):
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                self.verify_xml(xml)

    def test_nested_suite_counters_aggregate_without_double_counting(self):
        report = (
            '<testsuites tests="3" failures="0" errors="0" skipped="1">'
            '<testsuite tests="3" failures="0" errors="0" skipped="1">'
            '<testcase name="outer"/>'
            '<testsuite tests="2" failures="0" errors="0" skipped="1">'
            '<testcase name="inner"/>'
            '<testcase name="skip"><skipped/></testcase>'
            '</testsuite></testsuite></testsuites>'
        )
        result = self.verify_xml(report, min_tests=2)
        self.assertEqual(result["tests"], 3)
        self.assertEqual(result["executed"], 2)
        self.assertEqual(result["skipped"], 1)
        with self.assertRaises(ValueError):
            self.verify_xml(report.replace('tests="3"', 'tests="4"', 1), min_tests=2)
        with self.assertRaises(ValueError):
            self.verify_xml(report.replace('tests="2"', 'tests="1"', 1), min_tests=2)
