"""对等网络：配对、设备扫描与镜像同步。

两台电脑都运行同一个节点：
- 各自把本地文件夹作为「共享」开放给已配对设备；
- 对端用配对时协商出的令牌访问，权限由共享方按设备设置；
- 同步线程按镜像配置双向同步。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .paths import should_ignore
from .security import sha256_file
from .syncer import HubClient, HubError, SyncEngine
from .util import now_iso

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIR_ITERATIONS = 120_000
COMPANY_ITERATIONS = 150_000


# --------------------------------------------------------------------------- 公司（租户）密钥
def derive_company_key(code: str, company_id: str) -> bytes:
    salt = f"sharebox-company-key:{company_id}".encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", normalize_code(code).encode("utf-8"), salt,
                               COMPANY_ITERATIONS, dklen=32)


def company_verifier(code: str, company_id: str) -> str:
    salt = f"sharebox-company-verify:{company_id}".encode("utf-8")
    digest = hashlib.pbkdf2_hmac("sha256", normalize_code(code).encode("utf-8"), salt,
                                 COMPANY_ITERATIONS, dklen=32)
    return digest.hex()


def make_company_id() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(16))
    return "-".join(raw[index:index + 4] for index in range(0, 16, 4))


def bind_pair_key(company_key: bytes, pairing_key: bytes) -> bytes:
    """把公司密钥混入配对密钥：不同公司即使知道配对码也配不上。"""
    return hmac.new(company_key, pairing_key, hashlib.sha256).digest()


# --------------------------------------------------------------------------- 配对密码学
def derive_pair_key(code: str, node_a: str, node_b: str) -> bytes:
    salt = ":".join(sorted([node_a, node_b])).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", normalize_code(code).encode("utf-8"), salt, PAIR_ITERATIONS, dklen=32)


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def pair_proof(key: bytes, nonce: str) -> str:
    return hmac.new(key, f"proof:{nonce}".encode("utf-8"), hashlib.sha256).hexdigest()


def pair_ack(key: bytes, nonce: str) -> str:
    return hmac.new(key, f"ack:{nonce}".encode("utf-8"), hashlib.sha256).hexdigest()


def pair_token(key: bytes) -> str:
    return hmac.new(key, b"sharebox-peer-token", hashlib.sha256).hexdigest()


def make_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


class PairingState:
    """本机当前有效的配对码与握手随机数（仅内存，重启即失效）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.code: str | None = None
        self.expires: float = 0.0
        self.nonces: dict[str, float] = {}

    def new_code(self, ttl: int = 900) -> tuple[str, int]:
        code = make_code()
        with self._lock:
            self.code = code
            self.expires = time.time() + ttl
        return code, ttl

    def current(self) -> str | None:
        with self._lock:
            if not self.code or time.time() > self.expires:
                self.code = None
                return None
            return self.code

    def add_nonce(self, ttl: int = 180) -> str:
        nonce = secrets.token_hex(16)
        with self._lock:
            now = time.time()
            self.nonces = {k: v for k, v in self.nonces.items() if v > now}
            self.nonces[nonce] = now + ttl
        return nonce

    def use_nonce(self, nonce: str) -> bool:
        with self._lock:
            expiry = self.nonces.pop(nonce, None)
            return bool(expiry and expiry > time.time())

    def clear_code(self) -> None:
        with self._lock:
            self.code = None
            self.expires = 0.0


# --------------------------------------------------------------------------- 出站 HTTP
def post_json(url: str, payload: dict, timeout: float = 12.0) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "ShareBox-peer/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(body).get("error", body)
        except Exception:
            message = body
        raise HubError(exc.code, message or f"HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise HubError(0, f"无法连接对方：{exc}") from None


def normalize_address(address: str, port) -> tuple[str, int]:
    text = (address or "").strip()
    if text.startswith("http://"):
        text = text[7:]
    elif text.startswith("https://"):
        text = text[8:]
    text = text.rstrip("/")
    if "/" in text:
        text = text.split("/", 1)[0]
    if ":" in text and text.count(":") == 1:
        host, tail = text.split(":", 1)
        if tail.isdigit():
            return host.strip(), int(tail)
    if not text:
        raise ValueError("请填写对方地址")
    return text, int(port)


# --------------------------------------------------------------------------- 共享目录扫描
class ShareScanner(threading.Thread):
    """定期扫描「本地共享目录」，把新增/修改/删除登记成变更，供对端拉取。"""

    def __init__(self, app, interval: float = 8.0):
        super().__init__(daemon=True, name="sharebox-scanner")
        self.app = app
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.app.log.error(f"扫描共享目录失败：{exc}")
            self.stop_event.wait(self.interval)

    def tick(self) -> None:
        node = self.app.store.get_node()
        if not node:
            return
        for folder in self.app.store.all_folders():
            if not folder.get("local_path"):
                continue
            try:
                self.scan_folder(folder, node["owner_id"])
            except Exception as exc:  # noqa: BLE001
                self.app.log.warn(f"扫描 {folder['name']} 失败：{exc}")

    def scan_folder(self, folder: dict, owner_id: int) -> None:
        root = Path(folder["local_path"])
        if not root.is_dir():
            return
        existing = {row["path"]: row for row in self.app.store.list_files(folder["id"])}
        seen: set[str] = set()
        errors: list[OSError] = []

        for dirpath, dirnames, filenames in os.walk(root, onerror=errors.append):
            dirnames[:] = [d for d in dirnames if not should_ignore(d)]
            for name in filenames:
                if should_ignore(name) or name.endswith(".sbpart"):
                    continue
                full = Path(dirpath) / name
                try:
                    if full.is_symlink() or not full.is_file():
                        continue
                    stat = full.stat()
                    rel = full.relative_to(root).as_posix()
                except (OSError, ValueError):
                    continue
                seen.add(rel)
                old = existing.get(rel)
                if old and int(old["size"]) == stat.st_size and abs(float(old["mtime"]) - stat.st_mtime) < 0.001:
                    continue
                try:
                    digest = sha256_file(full)
                except OSError:
                    continue
                if old and old["sha256"] == digest:
                    self.app.store.update_file_mtime(folder["id"], rel, stat.st_mtime)
                    continue
                self.app.store.record_file(folder["id"], rel, stat.st_size, stat.st_mtime, digest, owner_id)
                self.app.store.audit(owner_id, "file.local", f"{folder['name']}/{rel}")

        if errors or not seen:
            return
        for rel in existing:
            if rel not in seen:
                self.app.store.record_delete(folder["id"], rel, owner_id)
                self.app.store.audit(owner_id, "file.local.delete", f"{folder['name']}/{rel}")


# --------------------------------------------------------------------------- 镜像同步
class MirrorService(threading.Thread):
    """按 mirrors 表把对端共享的文件夹同步到本地目录。"""

    def __init__(self, app, interval: float = 7.0):
        super().__init__(daemon=True, name="sharebox-mirrors")
        self.app = app
        self.interval = interval
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self.status: dict[int, dict] = {}

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.app.log.error(f"镜像同步失败：{exc}")
            self.stop_event.wait(self.interval)

    def tick(self) -> None:
        node = self.app.store.get_node()
        if not node:
            return
        for mirror in self.app.store.list_mirrors():
            if not mirror["enabled"]:
                continue
            self.sync_one(mirror, node)

    def sync_one(self, mirror: dict, node: dict) -> None:
        started = time.time()
        try:
            base = f"http://{mirror['address']}:{mirror['port']}"
            client = HubClient(base, mirror["token"], peer_node=node["node_id"])
            remote = {int(item["id"]): item for item in client.folders()}
            info = remote.get(int(mirror["remote_folder_id"]))
            if info is None:
                raise HubError(403, "对方已取消这个文件夹的共享")

            engine = SyncEngine(
                client,
                {str(mirror["remote_folder_id"]): mirror["local_path"]},
                Path(self.app.data_dir) / "mirrors" / str(mirror["id"]),
                self.app.log,
                device=node["device_name"],
            )
            engine.sync_folder(int(mirror["remote_folder_id"]), Path(mirror["local_path"]),
                               str(info.get("level", "read")), str(info.get("name", "")))
            with self._lock:
                self.status[int(mirror["id"])] = {
                    "last": now_iso(),
                    "error": "",
                    "up": engine.total["up"],
                    "down": engine.total["down"],
                    "seconds": round(time.time() - started, 1),
                }
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                current = self.status.get(int(mirror["id"]), {})
                self.status[int(mirror["id"])] = {
                    "last": current.get("last", ""),
                    "error": str(getattr(exc, "message", exc)),
                    "up": 0, "down": 0, "seconds": round(time.time() - started, 1),
                }

    def snapshot(self) -> dict[int, dict]:
        with self._lock:
            return {key: dict(value) for key, value in self.status.items()}
