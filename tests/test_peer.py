"""对等模式端到端测试：两个节点配对、授权、同步、日志、恢复码。"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from sharebox.hub import HubApp, HubServer
from sharebox.peer import MirrorService, ShareScanner
from sharebox.store import Store
from sharebox.syncer import HubClient, HubError
from sharebox.util import Logger

from tests.support import NullLog


class Node:
    def __init__(self, root: Path, name: str):
        self.name = name
        self.tmp = root / name
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.tmp)
        self.log = Logger(name, self.tmp / "test.log")
        self.app = HubApp(self.tmp, self.log)
        self.server = HubServer(("127.0.0.1", 0), self.app)
        self.port = self.server.server_address[1]
        self.app.port = self.port
        self.app.host = "127.0.0.1"
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.app.scanner = ShareScanner(self.app, interval=0.3)
        self.app.scanner.start()
        self.app.mirrors = MirrorService(self.app, interval=0.3)
        self.app.mirrors.start()
        self.client = None
        self.token = ""
        self.recovery = ""
        self.node_id = ""

    def setup(self, username: str, password: str, device: str, company_code: str = "Company123"):
        client = HubClient(self.url)
        data = client.request("POST", "/api/company/create", body=json.dumps({
            "company_name": "测试公司", "code": company_code,
            "username": username, "password": password, "device_name": device,
        }).encode(), headers={"Content-Type": "application/json"})
        self.token = data["token"]
        self.recovery = data["recovery_code"]
        self.node_id = data["node_id"]
        self.company_token = data["company_token"]
        self.company_id = data["company"]["company_id"]
        self.client = HubClient(self.url, self.token)
        return data

    def join(self, company_id: str, code: str, username: str, password: str, device: str):
        client = HubClient(self.url)
        data = client.request("POST", "/api/company/join", body=json.dumps({
            "company_id": company_id, "code": code, "company_name": "测试公司",
            "username": username, "password": password, "device_name": device,
        }).encode(), headers={"Content-Type": "application/json"})
        self.token = data["token"]
        self.recovery = data["recovery_code"]
        self.node_id = data["node_id"]
        self.company_token = data["company_token"]
        self.company_id = data["company"]["company_id"]
        self.client = HubClient(self.url, self.token)
        return data

    def post(self, path: str, payload: dict):
        return self.client.request("POST", path, body=json.dumps(payload).encode(),
                                   headers={"Content-Type": "application/json"})

    def put(self, path: str, payload: dict):
        return self.client.request("PUT", path, body=json.dumps(payload).encode(),
                                   headers={"Content-Type": "application/json"})

    def close(self):
        try:
            if self.app.scanner:
                self.app.scanner.stop_event.set()
            if self.app.mirrors:
                self.app.mirrors.stop_event.set()
            self.server.shutdown()
            self.server.server_close()
        finally:
            try:
                self.store.db.close()
            except Exception:
                pass
            try:
                self.log.close()
            except Exception:
                pass


def wait_until(predicate, timeout=20.0, interval=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


class PeerModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.a = Node(self.root, "node-a")
        self.b = Node(self.root, "node-b")
        self.a.setup("alice", "Alice1234", "张三的笔记本")
        self.b.join(self.a.company_id, "Company123", "bob", "Bob12345", "李四的笔记本")

    def tearDown(self):
        self.a.close()
        self.b.close()

    def pair(self):
        code = self.b.post("/api/device/code", {})["code"]
        self.a.post("/api/device/pair", {
            "address": "127.0.0.1", "port": self.b.port, "code": code,
        })
        peers_a = self.a.client.request("GET", "/api/peers")["peers"]
        peers_b = self.b.client.request("GET", "/api/peers")["peers"]
        self.assertEqual(len(peers_a), 1)
        self.assertEqual(len(peers_b), 1)
        self.assertEqual(peers_a[0]["name"], "李四的笔记本")
        self.assertEqual(peers_b[0]["name"], "张三的笔记本")
        return peers_a[0], peers_b[0]

    def share(self, peer_a):
        shared = self.root / "shared"
        shared.mkdir(exist_ok=True)
        (shared / "hello.txt").write_text("v1", encoding="utf-8")
        folder = self.a.post("/api/folders", {
            "name": "资料", "local_path": str(shared),
        })["folder"]
        self.a.put(f"/api/folders/{folder['id']}/perms", {
            "user_id": peer_a["user_id"], "level": "write",
        })
        return folder, shared

    def test_pairing_and_identity(self):
        peer_a, peer_b = self.pair()
        self.assertEqual(peer_a["address"], "127.0.0.1")
        self.assertEqual(peer_a["port"], self.b.port)
        self.assertTrue(peer_b["address"])
        folders = self.b.client.request("GET", f"/api/remote/{peer_b['id']}/folders")["folders"]
        self.assertEqual(folders, [])

    def test_pairing_requires_code(self):
        with self.assertRaises(HubError) as ctx:
            self.a.post("/api/device/pair", {
                "address": "127.0.0.1", "port": self.b.port, "code": "WRONGCODE",
            })
        self.assertIn(ctx.exception.status, (400, 403, 409, 502))

    def test_share_scan_and_mirror_sync(self):
        peer_a, peer_b = self.pair()
        folder, shared = self.share(peer_a)

        self.assertTrue(wait_until(lambda: len(
            self.a.client.request("GET", f"/api/folders/{folder['id']}/manifest")["files"]) == 1))

        remote = self.b.client.request("GET", f"/api/remote/{peer_b['id']}/folders")["folders"]
        self.assertEqual(len(remote), 1)
        self.assertEqual(remote[0]["level"], "write")

        mirror_local = self.root / "b-mirror"
        self.b.post("/api/mirrors", {
            "peer_id": peer_b["id"],
            "remote_folder_id": folder["id"],
            "local_path": str(mirror_local),
        })
        self.assertTrue(wait_until(lambda: (mirror_local / "hello.txt").exists()))
        self.assertEqual((mirror_local / "hello.txt").read_text(encoding="utf-8"), "v1")

        (shared / "hello.txt").write_text("v2", encoding="utf-8")
        self.assertTrue(wait_until(
            lambda: (mirror_local / "hello.txt").read_text(encoding="utf-8") == "v2", timeout=25))

        (mirror_local / "hello.txt").write_text("from-b", encoding="utf-8")
        self.assertTrue(wait_until(
            lambda: (shared / "hello.txt").read_text(encoding="utf-8") == "from-b", timeout=25))

    def test_audit_log_records_remote_changes(self):
        peer_a, peer_b = self.pair()
        folder, shared = self.share(peer_a)
        mirror_local = self.root / "b-mirror2"
        self.b.post("/api/mirrors", {
            "peer_id": peer_b["id"], "remote_folder_id": folder["id"],
            "local_path": str(mirror_local),
        })
        self.assertTrue(wait_until(
            lambda: (mirror_local / "hello.txt").read_text(encoding="utf-8") == "v1"))

        (mirror_local / "hello.txt").write_text("b-edit", encoding="utf-8")
        self.assertTrue(wait_until(
            lambda: (shared / "hello.txt").read_text(encoding="utf-8") == "b-edit", timeout=25))

        items = self.a.client.request("GET", "/api/logs?limit=100")["items"]
        uploads = [item for item in items if item["action"] == "file.upload"]
        self.assertTrue(uploads, f"没有记录到上传：{items}")
        self.assertIn("hello.txt", uploads[0]["detail"])
        self.assertEqual(uploads[0]["display_name"], "李四的笔记本")

    def test_different_company_cannot_pair(self):
        other = Node(self.root, "node-c")
        try:
            other.setup("carol", "Carol1234", "另一家公司", "OtherCompany99")
            code = other.post("/api/device/code", {})["code"]
            with self.assertRaises(HubError) as ctx:
                self.a.post("/api/device/pair", {
                    "address": "127.0.0.1", "port": other.port, "code": code,
                })
            self.assertIn(ctx.exception.status, (403, 502))
            self.assertEqual(self.a.client.request("GET", "/api/peers")["peers"], [])
        finally:
            other.close()

    def test_recovery_code_resets_password(self):
        client = HubClient(self.a.url)
        client.request("POST", "/api/recover", body=json.dumps({
            "code": self.a.recovery, "new_password": "NewPass123",
        }).encode(), headers={"Content-Type": "application/json"})
        fresh = HubClient(self.a.url)
        data = fresh.login("alice", "NewPass123", "test", self.a.company_token)
        self.assertTrue(data["token"])

    def test_readonly_peer_cannot_write(self):
        peer_a, peer_b = self.pair()
        shared = self.root / "readonly-share"
        shared.mkdir(exist_ok=True)
        (shared / "readme.txt").write_text("read only", encoding="utf-8")
        folder = self.a.post("/api/folders", {
            "name": "只读资料", "local_path": str(shared),
        })["folder"]
        self.a.put(f"/api/folders/{folder['id']}/perms", {
            "user_id": peer_a["user_id"], "level": "read",
        })
        mirror_local = self.root / "b-ro"
        self.b.post("/api/mirrors", {
            "peer_id": peer_b["id"], "remote_folder_id": folder["id"],
            "local_path": str(mirror_local),
        })
        self.assertTrue(wait_until(
            lambda: (mirror_local / "readme.txt").read_text(encoding="utf-8") == "read only"))

        (mirror_local / "readme.txt").write_text("hacked", encoding="utf-8")
        time.sleep(4)
        self.assertEqual((shared / "readme.txt").read_text(encoding="utf-8"), "read only")


if __name__ == "__main__":
    unittest.main()
