"""用打包后的 exe 跑一遍真实双机配对（验收安装包）。

用法：python tools/frozen_peer_check.py [安装包路径]
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sharebox.syncer import HubClient  # noqa: E402

SETUP = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "ShareBox-Setup-2.1.0.exe"


def free_ports(count: int) -> list[int]:
    ports = []
    for _ in range(count):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
    return ports


def wait_health(url: str, timeout: float = 30.0) -> None:
    client = HubClient(url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            client.request("GET", "/api/health")
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"启动超时: {url}")


def post(client: HubClient, path: str, payload: dict) -> dict:
    return client.request("POST", path, body=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})


def put(client: HubClient, path: str, payload: dict) -> dict:
    return client.request("PUT", path, body=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})


def main() -> int:
    if not SETUP.exists():
        print(f"找不到安装包：{SETUP}")
        return 1
    base = Path(tempfile.mkdtemp(prefix="sb-frozen-peer-"))
    node_a, node_b = base / "a", base / "b"
    port_a, port_b = free_ports(2)
    processes: list[subprocess.Popen] = []
    try:
        for target in (node_a, node_b):
            result = subprocess.run([str(SETUP), "--install-silent", "--target", str(target),
                                     "--no-shortcuts", "--no-desktop"],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise RuntimeError(result.stdout + result.stderr)
            print(f"已安装：{target}")

        for exe, port, data in ((node_a / "ShareBox.exe", port_a, base / "data-a"),
                                (node_b / "ShareBox.exe", port_b, base / "data-b")):
            processes.append(subprocess.Popen(
                [str(exe), "hub", "--host", "127.0.0.1", "--port", str(port), "--data", str(data)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
        url_a, url_b = f"http://127.0.0.1:{port_a}", f"http://127.0.0.1:{port_b}"
        wait_health(url_a)
        wait_health(url_b)
        print("两个节点已启动")

        a = HubClient(url_a)
        b = HubClient(url_b)
        setup_a = post(a, "/api/company/create", {
            "company_name": "验收公司", "code": "Accept123",
            "username": "owner", "password": "Owner1234", "device_name": "A 笔记本",
        })
        a.token = setup_a["token"]
        setup_b = post(b, "/api/company/join", {
            "company_id": setup_a["company"]["company_id"], "code": "Accept123",
            "company_name": "验收公司",
            "username": "owner", "password": "Owner1234", "device_name": "B 笔记本",
        })
        b.token = setup_b["token"]

        code = post(b, "/api/device/code", {})["code"]
        paired = post(a, "/api/device/pair", {"address": "127.0.0.1", "port": port_b, "code": code})
        print(f"配对成功：{paired['peer']['name']}")

        share_dir = base / "shared"
        share_dir.mkdir()
        (share_dir / "hello.txt").write_text("frozen-v1", encoding="utf-8")
        folder = post(a, "/api/folders", {"name": "共享资料", "local_path": str(share_dir)})["folder"]
        peers_a = a.request("GET", "/api/peers")["peers"]
        put(a, f"/api/folders/{folder['id']}/perms",
            {"user_id": peers_a[0]["user_id"], "level": "write"})

        peers_b = b.request("GET", "/api/peers")["peers"]
        remote = b.request("GET", f"/api/remote/{peers_b[0]['id']}/folders")["folders"]
        if not remote:
            raise RuntimeError("对方没有返回共享文件夹")
        print(f"看到对方共享：{remote[0]['name']}（{remote[0]['level']}）")

        mirror = base / "mirror"
        post(b, "/api/mirrors", {"peer_id": peers_b[0]["id"],
                                 "remote_folder_id": folder["id"],
                                 "local_path": str(mirror)})
        deadline = time.time() + 40
        while time.time() < deadline:
            target = mirror / "hello.txt"
            if target.exists() and target.read_text(encoding="utf-8") == "frozen-v1":
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("镜像同步超时")
        print("镜像同步成功：hello.txt")
        logs = a.request("GET", "/api/logs?limit=50")["items"]
        print(f"日志条数：{len(logs)}")
        print("\n打包版双机对等验收通过")
        return 0
    finally:
        for process in processes:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True)
        time.sleep(1.5)
        for target in (node_a, node_b):
            exe = target / "ShareBox.exe"
            if exe.exists():
                subprocess.run([str(exe), "--uninstall-silent"], capture_output=True, timeout=60)
        time.sleep(1)
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
