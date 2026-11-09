import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from sharebox.syncer import HubClient, SyncEngine

from tests.support import HubFixture, NullLog

FOLDER_BODY = b'{"name":"team-share"}'


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.fix = HubFixture()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.admin = HubClient(self.fix.url)
        self.admin.login("admin", "Admin1234", "admin-pc", self.fix.company_token)
        self.folder = self.admin.request(
            "POST", "/api/folders", body=FOLDER_BODY,
            headers={"Content-Type": "application/json"},
        )["folder"]
        self.folder_id = self.folder["id"]

    def tearDown(self):
        self.tmp.cleanup()
        self.fix.close()

    # ------------------------------------------------------------- 工具
    def add_user(self, name, level="write"):
        password = "User1234"
        self.admin.request("POST", "/api/users", body=json.dumps({
            "username": name, "password": password, "display_name": name,
        }).encode(), headers={"Content-Type": "application/json"})
        users = self.admin.request("GET", "/api/users")["users"]
        uid = next(u["id"] for u in users if u["username"] == name)
        self.admin.request("PUT", f"/api/folders/{self.folder_id}/perms",
                           body=json.dumps({"user_id": uid, "level": level}).encode(),
                           headers={"Content-Type": "application/json"})
        return password

    def engine(self, name, local: Path, device: str, password="User1234"):
        local.mkdir(parents=True, exist_ok=True)
        client = HubClient(self.fix.url)
        client.login(name, password, device, self.fix.company_token)
        return SyncEngine(
            client, {str(self.folder_id): str(local)}, self.root / f"state-{name}",
            NullLog(), device=device,
        )

    def server_sha(self):
        files = self.admin.manifest(self.folder_id, 0)["files"]
        return {f["path"]: f["sha256"] for f in files}

    # ------------------------------------------------------------- 测试
    def test_two_way_sync(self):
        self.add_user("alice")
        self.add_user("bob")
        a = self.engine("alice", self.root / "a", "ALICE-PC")
        b = self.engine("bob", self.root / "b", "BOB-PC")

        (self.root / "a" / "报告.txt").write_text("v1", encoding="utf-8")
        a.sync_all()
        b.sync_all()
        self.assertEqual((self.root / "b" / "报告.txt").read_text(encoding="utf-8"), "v1")

        (self.root / "b" / "报告.txt").write_text("v2", encoding="utf-8")
        b.sync_all()
        a.sync_all()
        self.assertEqual((self.root / "a" / "报告.txt").read_text(encoding="utf-8"), "v2")

    def test_nested_folders_sync(self):
        self.add_user("alice")
        a = self.engine("alice", self.root / "a", "ALICE-PC")
        deep = self.root / "a" / "2026" / "预算"
        deep.mkdir(parents=True)
        (deep / "一季度.xlsx").write_text("sheet", encoding="utf-8")
        a.sync_all()

        self.add_user("bob")
        b = self.engine("bob", self.root / "b", "BOB-PC")
        b.sync_all()
        self.assertEqual(
            (self.root / "b" / "2026" / "预算" / "一季度.xlsx").read_text(encoding="utf-8"), "sheet")

    def test_conflict_creates_local_copy(self):
        self.add_user("alice")
        self.add_user("bob")
        a = self.engine("alice", self.root / "a", "ALICE-PC")
        b = self.engine("bob", self.root / "b", "BOB-PC")

        (self.root / "a" / "报告.txt").write_text("base", encoding="utf-8")
        a.sync_all()
        b.sync_all()

        (self.root / "a" / "报告.txt").write_text("A-edit", encoding="utf-8")
        (self.root / "b" / "报告.txt").write_text("B-edit", encoding="utf-8")
        a.sync_all()
        b.sync_all()

        self.assertEqual((self.root / "b" / "报告.txt").read_text(encoding="utf-8"), "A-edit")
        conflicts = [p for p in (self.root / "b").iterdir() if "冲突副本" in p.name]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].read_text(encoding="utf-8"), "B-edit")

        a.sync_all()
        conflicts_a = [p for p in (self.root / "a").iterdir() if "冲突副本" in p.name]
        self.assertEqual(len(conflicts_a), 1)
        self.assertEqual(conflicts_a[0].read_text(encoding="utf-8"), "B-edit")

    def test_delete_propagates(self):
        self.add_user("alice")
        self.add_user("bob")
        a = self.engine("alice", self.root / "a", "ALICE-PC")
        b = self.engine("bob", self.root / "b", "BOB-PC")

        (self.root / "a" / "临时.txt").write_text("temp", encoding="utf-8")
        a.sync_all()
        b.sync_all()
        self.assertTrue((self.root / "b" / "临时.txt").exists())

        (self.root / "a" / "临时.txt").unlink()
        a.sync_all()
        b.sync_all()
        self.assertFalse((self.root / "b" / "临时.txt").exists())
        self.assertEqual(self.server_sha(), {})

    def test_readonly_cannot_upload(self):
        self.add_user("alice", "write")
        self.add_user("bob", "read")
        a = self.engine("alice", self.root / "a", "ALICE-PC")
        b = self.engine("bob", self.root / "b", "BOB-PC")

        (self.root / "a" / "只读.txt").write_text("server-version", encoding="utf-8")
        a.sync_all()
        b.sync_all()
        self.assertEqual((self.root / "b" / "只读.txt").read_text(encoding="utf-8"), "server-version")

        (self.root / "b" / "只读.txt").write_text("local-hack", encoding="utf-8")
        b.sync_all()

        expected = hashlib.sha256(b"server-version").hexdigest()
        self.assertEqual(self.server_sha()["只读.txt"], expected)
        self.assertEqual((self.root / "b" / "只读.txt").read_text(encoding="utf-8"), "local-hack")

    def test_empty_dir_syncs(self):
        self.add_user("alice")
        a = self.engine("alice", self.root / "a", "ALICE-PC")
        self.admin.mkdir(self.folder_id, "空目录")

        a.sync_all()
        self.assertTrue((self.root / "a" / "空目录").is_dir())

        self.admin.request("DELETE",
                           f"/api/folders/{self.folder_id}/dir?path=%E7%A9%BA%E7%9B%AE%E5%BD%95")
        a.sync_all()
        self.assertFalse((self.root / "a" / "空目录").exists())


if __name__ == "__main__":
    unittest.main()
