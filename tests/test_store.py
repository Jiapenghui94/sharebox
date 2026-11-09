import tempfile
import unittest
from pathlib import Path

from sharebox.store import Store, StoreError


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.admin = self.store.create_user("admin", "Admin1234", "管理员", "admin")
        self.alice = self.store.create_user("alice", "Alice1234", "爱丽丝", "member")
        self.bob = self.store.create_user("bob", "Bob12345", "鲍勃", "member")

    def tearDown(self):
        try:
            self.store.db.close()
        finally:
            self.tmp.cleanup()

    def test_duplicate_username_rejected(self):
        with self.assertRaises(StoreError):
            self.store.create_user("ALICE", "Another123", "", "member")

    def test_weak_password_rejected(self):
        with self.assertRaises(StoreError):
            self.store.create_user("weak", "12345678", "", "member")

    def test_authentication(self):
        self.assertIsNotNone(self.store.authenticate("alice", "Alice1234"))
        self.assertIsNone(self.store.authenticate("alice", "wrong-pass"))
        self.assertIsNone(self.store.authenticate("nobody", "Alice1234"))

    def test_inactive_user_cannot_authenticate(self):
        self.store.set_active(self.alice["id"], False)
        self.assertIsNone(self.store.authenticate("alice", "Alice1234"))

    def test_folder_permissions(self):
        folder = self.store.create_folder("项目文档", self.admin["id"])
        self.assertEqual(self.store.folder_level(self.admin, folder), "admin")
        self.assertIsNone(self.store.folder_level(self.alice, folder))

        self.store.set_perm(folder["id"], self.alice["id"], "read")
        folder = self.store.get_folder(folder["id"])
        self.assertEqual(self.store.folder_level(self.alice, folder), "read")

        self.store.set_perm(folder["id"], self.alice["id"], "write")
        folder = self.store.get_folder(folder["id"])
        self.assertEqual(self.store.folder_level(self.alice, folder), "write")

        self.store.set_perm(folder["id"], self.alice["id"], None)
        folder = self.store.get_folder(folder["id"])
        self.assertIsNone(self.store.folder_level(self.alice, folder))

    def test_owner_implicit_write(self):
        folder = self.store.create_folder("alice 的共享", self.alice["id"])
        self.assertEqual(self.store.folder_level(self.alice, folder), "write")
        self.assertEqual(self.store.folder_level(self.bob, folder), None)

    def test_manifest_incremental(self):
        folder = self.store.create_folder("docs", self.admin["id"])
        self.assertEqual(self.store.manifest(folder["id"], 0)["files"], [])

        seq1 = self.store.record_file(folder["id"], "a.txt", 3, 1000.0, "aa", self.admin["id"])
        seq2 = self.store.record_file(folder["id"], "b.txt", 4, 1001.0, "bb", self.admin["id"])

        full = self.store.manifest(folder["id"], 0)
        self.assertEqual(len(full["files"]), 2)

        delta = self.store.manifest(folder["id"], seq1)
        self.assertEqual([c["path"] for c in delta["changes"]], ["b.txt"])
        self.assertEqual([f["path"] for f in delta["files"]], ["b.txt"])

        self.store.record_delete(folder["id"], "a.txt", self.admin["id"])
        delta = self.store.manifest(folder["id"], seq2)
        self.assertEqual(delta["changes"][0]["action"], "delete")
        self.assertEqual(delta["files"], [])
        self.assertEqual(len(self.store.manifest(folder["id"], 0)["files"]), 1)

    def test_dir_tracking(self):
        folder = self.store.create_folder("docs", self.admin["id"])
        self.store.record_dir(folder["id"], "空目录", False, self.admin["id"])
        self.assertEqual(self.store.list_dirs(folder["id"]), ["空目录"])
        self.assertTrue(self.store.dir_exists(folder["id"], "空目录"))
        self.store.record_dir(folder["id"], "空目录", True, self.admin["id"])
        self.assertEqual(self.store.list_dirs(folder["id"]), [])

    def test_session_lifecycle(self):
        session = self.store.create_session(self.alice["id"], 1, "test")
        self.assertIsNotNone(self.store.user_for_token(session["token"]))
        self.store.delete_session(session["token"])
        self.assertIsNone(self.store.user_for_token(session["token"]))

    def test_password_reset_invalidates_sessions(self):
        session = self.store.create_session(self.alice["id"], 1, "test")
        self.store.set_password(self.alice["id"], "NewPass123")
        self.assertIsNone(self.store.user_for_token(session["token"]))
        self.assertIsNotNone(self.store.authenticate("alice", "NewPass123"))


if __name__ == "__main__":
    unittest.main()
