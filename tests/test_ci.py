from pathlib import Path
import tempfile
import unittest

from ermik.ci import verify_junit


class JUnitTests(unittest.TestCase):
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
