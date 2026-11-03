"""Exact Git tree mode checks in disposable repositories."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ermik.git_tree import TreeError, verify_changed_tree


@unittest.skipUnless(shutil.which("git"), "Git is required")
class GitTreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.write("src/original.txt", "first\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *args, input=None):
        return subprocess.run(["git", "-C", str(self.repo), *args], input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=True).stdout.decode().strip()

    def write(self, name, value):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def staged_mode(self, name, mode, content=None, sha=None):
        if sha is None:
            sha = self.git("hash-object", "-w", "--stdin", input=content.encode())
        self.git("update-index", "--add", "--cacheinfo", f"{mode},{sha},{name}")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_regular_change_and_rename(self):
        self.write("src/original.txt", "second\n")
        head = self.commit()
        self.assertEqual(verify_changed_tree(self.repo, self.base, head),
                         ("src/original.txt",))
        self.git("mv", "src/original.txt", "src/renamed.txt")
        renamed = self.commit()
        self.assertEqual(verify_changed_tree(self.repo, head, renamed),
                         ("src/renamed.txt",))
        with self.assertRaises(TreeError):
            verify_changed_tree(self.repo, head, renamed,
                                allowed_paths=("src/renamed.txt",))
        self.assertEqual(verify_changed_tree(self.repo, head, renamed,
                                             allowed_paths=("src",)),
                         ("src/renamed.txt",))

    def test_reject_symlink_leaf_and_parent(self):
        self.git("rm", "-q", "src/original.txt")
        head = self.staged_mode("src/original.txt", "120000", "../../outside")
        with self.assertRaises(TreeError):
            verify_changed_tree(self.repo, self.base, head)

        self.git("reset", "--hard", self.base)
        self.git("rm", "-q", "src/original.txt")
        parent = self.staged_mode("src", "120000", "outside")
        with self.assertRaises(TreeError):
            verify_changed_tree(self.repo, self.base, parent)

    def test_reject_gitlink(self):
        head = self.staged_mode("vendor", "160000", sha=self.base)
        with self.assertRaises(TreeError):
            verify_changed_tree(self.repo, self.base, head)

    def test_reject_wrong_checkout_head(self):
        self.write("src/original.txt", "second\n")
        head = self.commit()
        self.git("checkout", "-q", self.base)
        with self.assertRaises(TreeError):
            verify_changed_tree(self.repo, self.base, head)

    def test_reject_more_than_100_changed_files(self):
        for index in range(101):
            self.write(f"src/f{index:03d}.txt", "x")
        head = self.commit()
        with self.assertRaises(TreeError):
            verify_changed_tree(self.repo, self.base, head)

    def test_deletion_inspects_base_mode(self):
        self.git("rm", "-q", "src/original.txt")
        head = self.commit()
        self.assertEqual(verify_changed_tree(self.repo, self.base, head),
                         ("src/original.txt",))


if __name__ == "__main__":
    unittest.main()
