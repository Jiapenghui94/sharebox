import unittest

from sharebox.paths import UnsafePath, normalize_relpath, resolve_under, should_ignore


class PathSafetyTest(unittest.TestCase):
    def test_normalizes_backslashes(self):
        self.assertEqual(normalize_relpath("a\\b\\c.txt"), "a/b/c.txt")

    def test_strips_redundant_segments(self):
        self.assertEqual(normalize_relpath("./a//b/./c.txt"), "a/b/c.txt")

    def test_rejects_parent_traversal(self):
        for bad in ("../etc/passwd", "a/../../b", "..", "a/.."):
            with self.assertRaises(UnsafePath, msg=bad):
                normalize_relpath(bad)

    def test_rejects_absolute_and_drive(self):
        for bad in ("/etc/passwd", "C:\\Windows\\win.ini", "c:temp", "\\\\server\\share"):
            with self.assertRaises(UnsafePath, msg=bad):
                normalize_relpath(bad)

    def test_rejects_windows_reserved_chars(self):
        with self.assertRaises(UnsafePath):
            normalize_relpath("a/b:c.txt")
        with self.assertRaises(UnsafePath):
            normalize_relpath("a/b?c.txt")

    def test_rejects_empty(self):
        for bad in ("", "   ", "/", "./"):
            with self.assertRaises(UnsafePath, msg=repr(bad)):
                normalize_relpath(bad)

    def test_resolve_under_stays_inside(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(resolve_under(root, "a/b.txt"), root.resolve() / "a" / "b.txt")
            with self.assertRaises(UnsafePath):
                resolve_under(root, "../escape.txt")

    def test_ignore_patterns(self):
        self.assertTrue(should_ignore("~$报告.docx"))
        self.assertTrue(should_ignore("Thumbs.db"))
        self.assertTrue(should_ignore(".sharebox"))
        self.assertFalse(should_ignore("报告.docx"))


if __name__ == "__main__":
    unittest.main()
