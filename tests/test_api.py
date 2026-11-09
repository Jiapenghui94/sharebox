import json
import tempfile
import unittest
from pathlib import Path

from sharebox.syncer import AuthExpired, HubClient, HubError

from tests.support import HubFixture


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.fix = HubFixture()
        self.tmp = tempfile.TemporaryDirectory()
        self.admin = HubClient(self.fix.url)
        self.admin.login("admin", "Admin1234", "admin-pc", self.fix.company_token)

    def tearDown(self):
        self.tmp.cleanup()
        self.fix.close()

    # ------------------------------------------------------------- 工具
    def make_user(self, username, password="User1234", role="member"):
        self.admin.request("POST", "/api/users", body=json.dumps({
            "username": username, "password": password, "display_name": username, "role": role,
        }).encode(), headers={"Content-Type": "application/json"})
        client = HubClient(self.fix.url)
        client.login(username, password, "pc-" + username, self.fix.company_token)
        return client

    def user_id(self, username):
        users = self.admin.request("GET", "/api/users")["users"]
        return next(u["id"] for u in users if u["username"] == username)

    def upload_text(self, client, folder_id, rel, text):
        path = Path(self.tmp.name) / ("up-" + rel.replace("/", "_"))
        path.write_text(text, encoding="utf-8")
        return client.upload(folder_id, rel, path)

    def download_text(self, client, folder_id, rel, sha=None):
        dest = Path(self.tmp.name) / ("down-" + rel.replace("/", "_"))
        client.download_to(folder_id, rel, dest, sha)
        return dest.read_text(encoding="utf-8")

    # ------------------------------------------------------------- 测试
    def test_health_is_public(self):
        client = HubClient(self.fix.url)
        data = client.request("GET", "/api/health")
        self.assertTrue(data["ok"])

    def test_login_failure(self):
        client = HubClient(self.fix.url)
        with self.assertRaises(HubError) as ctx:
            client.login("admin", "wrong-password")
        self.assertEqual(ctx.exception.status, 401)

    def test_requires_auth(self):
        client = HubClient(self.fix.url)
        with self.assertRaises(AuthExpired):
            client.folders()

    def test_folder_upload_download_cycle(self):
        folder = self.admin.request("POST", "/api/folders", body=__import__("json").dumps(
            {"name": "项目文档"}).encode(), headers={"Content-Type": "application/json"})["folder"]

        self.upload_text(self.admin, folder["id"], "2026/计划.txt", "第一版")
        after_first = self.admin.manifest(folder["id"], 0)
        self.assertEqual(len(after_first["files"]), 1)
        self.assertEqual(after_first["files"][0]["path"], "2026/计划.txt")

        text = self.download_text(self.admin, folder["id"], "2026/计划.txt",
                                  after_first["files"][0]["sha256"])
        self.assertEqual(text, "第一版")

        self.upload_text(self.admin, folder["id"], "2026/计划.txt", "第二版")
        delta = self.admin.manifest(folder["id"], after_first["seq"])
        self.assertEqual([c["path"] for c in delta["changes"]], ["2026/计划.txt"])
        self.assertEqual(self.download_text(self.admin, folder["id"], "2026/计划.txt"), "第二版")

    def test_path_traversal_rejected(self):
        folder = self.admin.request("POST", "/api/folders", body=b'{"name":"sec"}',
                                    headers={"Content-Type": "application/json"})["folder"]
        path = Path(self.tmp.name) / "evil.txt"
        path.write_text("x", encoding="utf-8")
        for bad in ("../evil.txt", "a/../../evil.txt", "C:\\Windows\\evil.txt"):
            with self.assertRaises(HubError) as ctx:
                self.admin.upload(folder["id"], bad, path)
            self.assertEqual(ctx.exception.status, 400, bad)

        with self.assertRaises(HubError) as ctx:
            self.admin.request("GET", f"/api/folders/{folder['id']}/file?path=..%2F..%2Fsharebox.db")
        self.assertEqual(ctx.exception.status, 400)

    def test_permission_enforcement(self):
        folder = self.admin.request("POST", "/api/folders", body=b'{"name":"team"}',
                                    headers={"Content-Type": "application/json"})["folder"]
        alice = self.make_user("alice")
        bob = self.make_user("bob")
        alice_id = self.user_id("alice")

        self.assertEqual(alice.folders(), [])
        with self.assertRaises(HubError) as ctx:
            alice.manifest(folder["id"], 0)
        self.assertEqual(ctx.exception.status, 403)

        self.admin.request("PUT", f"/api/folders/{folder['id']}/perms",
                           body=json.dumps({"user_id": alice_id, "level": "read"}).encode(),
                           headers={"Content-Type": "application/json"})
        listed = alice.folders()
        self.assertEqual(listed[0]["level"], "read")

        path = Path(self.tmp.name) / "ro.txt"
        path.write_text("hello", encoding="utf-8")
        with self.assertRaises(HubError) as ctx:
            alice.upload(folder["id"], "ro.txt", path)
        self.assertEqual(ctx.exception.status, 403)

        self.upload_text(self.admin, folder["id"], "ro.txt", "hello")
        self.assertEqual(self.download_text(alice, folder["id"], "ro.txt"), "hello")

        # 提升为读写后可以上传
        self.admin.request("PUT", f"/api/folders/{folder['id']}/perms",
                           body=json.dumps({"user_id": alice_id, "level": "write"}).encode(),
                           headers={"Content-Type": "application/json"})
        self.upload_text(alice, folder["id"], "alice.txt", "from alice")
        paths = {f["path"] for f in alice.manifest(folder["id"], 0)["files"]}
        self.assertIn("alice.txt", paths)

        # bob 未获授权，仍然看不到
        self.assertEqual(bob.folders(), [])

    def test_delete_and_dirs(self):
        folder = self.admin.request("POST", "/api/folders", body=b'{"name":"dirs"}',
                                    headers={"Content-Type": "application/json"})["folder"]
        self.upload_text(self.admin, folder["id"], "a.txt", "a")
        self.admin.mkdir(folder["id"], "空目录")

        manifest = self.admin.manifest(folder["id"], 0)
        self.assertIn("空目录", manifest["dirs"])

        self.admin.delete(folder["id"], "a.txt")
        self.assertEqual(self.admin.manifest(folder["id"], 0)["files"], [])

        self.admin.request("DELETE", f"/api/folders/{folder['id']}/dir?path=%E7%A9%BA%E7%9B%AE%E5%BD%95")
        self.assertEqual(self.admin.manifest(folder["id"], 0)["dirs"], [])

    def test_non_empty_dir_cannot_be_deleted(self):
        folder = self.admin.request("POST", "/api/folders", body=b'{"name":"dirs2"}',
                                    headers={"Content-Type": "application/json"})["folder"]
        self.admin.mkdir(folder["id"], "sub")
        self.upload_text(self.admin, folder["id"], "sub/a.txt", "a")
        with self.assertRaises(HubError) as ctx:
            self.admin.request("DELETE", f"/api/folders/{folder['id']}/dir?path=sub")
        self.assertEqual(ctx.exception.status, 409)

    def test_only_admin_manages_users(self):
        alice = self.make_user("alice")
        with self.assertRaises(HubError) as ctx:
            alice.request("POST", "/api/users", body=b'{"username":"x","password":"Xyz12345"}',
                          headers={"Content-Type": "application/json"})
        self.assertEqual(ctx.exception.status, 403)

    def test_last_admin_cannot_be_demoted(self):
        with self.assertRaises(HubError) as ctx:
            self.admin.request("PATCH", "/api/users/1", body=b'{"role":"member"}',
                               headers={"Content-Type": "application/json"})
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
