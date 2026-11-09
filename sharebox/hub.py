"""中心服务：HTTP API + 内置 Web 界面。"""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import __version__
from .paths import UnsafePath, normalize_relpath
from .peer import (COMPANY_ITERATIONS, MirrorService, PairingState, ShareScanner,
                   bind_pair_key, company_verifier, derive_company_key, derive_pair_key,
                   make_company_id, normalize_address, normalize_code, pair_ack, pair_proof,
                   pair_token, post_json)
from .security import hash_password, password_problem, verify_password
from .storage import FolderStorage, ShareFS
from .store import Store, StoreError
from .util import local_ips, now_iso, parse_iso, safe_filename
from .syncer import HubClient, HubError

MAX_JSON_BODY = 1 << 20
CHUNK = 256 * 1024

RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_recovery_code() -> str:
    raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(20))
    return "-".join(raw[index:index + 4] for index in range(0, 20, 4))


def peer_public(peer: dict) -> dict:
    return {
        "id": peer["id"], "node_id": peer["node_id"], "name": peer["name"],
        "address": peer["address"], "port": peer["port"], "user_id": peer.get("user_id"),
        "last_seen": peer.get("last_seen") or "", "last_ip": peer.get("last_ip") or "",
    }


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _public_user(user: dict) -> dict:
    return {k: user[k] for k in ("id", "username", "display_name", "role", "active", "created_at")
            if k in user}


ROUTES = [
    ("GET", re.compile(r"^/api/company/status$"), "company_status"),
    ("POST", re.compile(r"^/api/company/create$"), "company_create"),
    ("POST", re.compile(r"^/api/company/join$"), "company_join"),
    ("POST", re.compile(r"^/api/company/verify$"), "company_verify"),
    ("GET", re.compile(r"^/api/company$"), "company_info"),
    ("POST", re.compile(r"^/api/recover$"), "recover"),
    ("POST", re.compile(r"^/api/pair/hello$"), "pair_hello"),
    ("POST", re.compile(r"^/api/pair/confirm$"), "pair_confirm"),
    ("POST", re.compile(r"^/api/login$"), "login"),
    ("GET", re.compile(r"^/api/health$"), "health"),
    ("POST", re.compile(r"^/api/logout$"), "logout"),
    ("GET", re.compile(r"^/api/me$"), "me"),
    ("POST", re.compile(r"^/api/password$"), "change_password"),
    ("GET", re.compile(r"^/api/account$"), "account"),
    ("POST", re.compile(r"^/api/account/password$"), "account_password"),
    ("GET", re.compile(r"^/api/account/recovery$"), "account_recovery"),
    ("POST", re.compile(r"^/api/account/device$"), "account_device"),
    ("POST", re.compile(r"^/api/pick-folder$"), "pick_folder"),
    ("POST", re.compile(r"^/api/device/code$"), "device_code"),
    ("POST", re.compile(r"^/api/device/pair$"), "device_pair"),
    ("GET", re.compile(r"^/api/peers$"), "list_peers"),
    ("DELETE", re.compile(r"^/api/peers/(\d+)$"), "delete_peer"),
    ("GET", re.compile(r"^/api/remote/(\d+)/folders$"), "remote_folders"),
    ("GET", re.compile(r"^/api/mirrors$"), "list_mirrors"),
    ("POST", re.compile(r"^/api/mirrors$"), "add_mirror"),
    ("DELETE", re.compile(r"^/api/mirrors/(\d+)$"), "delete_mirror"),
    ("POST", re.compile(r"^/api/mirrors/(\d+)/toggle$"), "toggle_mirror"),
    ("GET", re.compile(r"^/api/logs$"), "logs"),
    ("GET", re.compile(r"^/api/folders$"), "list_folders"),
    ("POST", re.compile(r"^/api/folders$"), "create_folder"),
    ("GET", re.compile(r"^/api/folders/(\d+)$"), "get_folder"),
    ("DELETE", re.compile(r"^/api/folders/(\d+)$"), "delete_folder"),
    ("GET", re.compile(r"^/api/folders/(\d+)/manifest$"), "manifest"),
    ("GET", re.compile(r"^/api/folders/(\d+)/file$"), "download"),
    ("PUT", re.compile(r"^/api/folders/(\d+)/file$"), "upload"),
    ("DELETE", re.compile(r"^/api/folders/(\d+)/file$"), "delete_file"),
    ("POST", re.compile(r"^/api/folders/(\d+)/mkdir$"), "mkdir"),
    ("DELETE", re.compile(r"^/api/folders/(\d+)/dir$"), "delete_dir"),
    ("GET", re.compile(r"^/api/folders/(\d+)/perms$"), "get_perms"),
    ("PUT", re.compile(r"^/api/folders/(\d+)/perms$"), "put_perm"),
    ("GET", re.compile(r"^/api/users$"), "list_users"),
    ("POST", re.compile(r"^/api/users$"), "create_user"),
    ("PATCH", re.compile(r"^/api/users/(\d+)$"), "patch_user"),
    ("GET", re.compile(r"^/api/audit$"), "audit_log"),
]

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class HubApp:
    def __init__(self, data_dir, log, max_upload_mb: int = 2048, web_dir: Path | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.store = Store(self.data_dir)
        self.storage = FolderStorage(self.data_dir)
        self.max_bytes = max(1, int(max_upload_mb)) * 1024 * 1024
        self.web_dir = Path(web_dir) if web_dir else Path(__file__).parent / "web"
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._pair_hits: dict[str, list[float]] = {}
        self.pairing = PairingState()
        self.node: dict | None = None
        self.owner: dict | None = None
        self.company_key: bytes | None = None
        self.company_tokens: dict[str, float] = {}
        self.port = 8787
        self.host = "0.0.0.0"
        self.scanner: ShareScanner | None = None
        self.mirrors: MirrorService | None = None
        self.refresh_node()

    # ------------------------------------------------------------- 公司
    def issue_company_token(self) -> str:
        import secrets as _secrets
        token = _secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self.company_tokens = {k: v for k, v in self.company_tokens.items() if v > now}
            self.company_tokens[token] = now + 12 * 3600
        return token

    def company_token_valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            expiry = self.company_tokens.get(token)
            if not expiry or expiry <= time.time():
                self.company_tokens.pop(token, None)
                return False
            return True

    # ------------------------------------------------------------- 节点
    def refresh_node(self) -> None:
        self.node = self.store.get_node()
        self.owner = self.store.get_user(self.node["owner_id"]) if self.node else None

    def needs_setup(self) -> bool:
        return self.store.get_node() is None

    def fs(self, folder: dict) -> ShareFS:
        return ShareFS(self.storage, folder)

    def pair_allowed(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._pair_hits.get(ip, []) if now - t < 60]
            self._pair_hits[ip] = hits
            if len(hits) >= 20:
                return False
            hits.append(now)
            return True

    # ------------------------------------------------------------- 登录限速
    def rate_remaining(self, key: str) -> int:
        now = time.time()
        with self._lock:
            items = [t for t in self._failures.get(key, []) if now - t < 300]
            self._failures[key] = items
            if len(items) >= 5:
                return max(1, int(60 - (now - items[-1])))
            return 0

    def rate_fail(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.time())

    def rate_ok(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "ShareBox/" + __version__
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> HubApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # noqa: N802
        self.app.log.debug("%s %s" % (self.address_string(), fmt % args))

    # ----------------------------------------------------------------- 入口
    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")

    def do_PATCH(self):  # noqa: N802
        self._dispatch("PATCH")

    def _dispatch(self, method: str):
        self._body_read = False
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if len(path) > 1 and path.endswith("/"):
                path = path.rstrip("/")
            query = parse_qs(parsed.query, keep_blank_values=True)
            if method == "GET" and self._serve_static(path):
                return
            for route_method, pattern, name in ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if match:
                    getattr(self, "api_" + name)(match.groups(), query)
                    return
            raise ApiError(404, "接口不存在")
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except UnsafePath as exc:
            self._error(400, f"路径不合法：{exc}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception:
            self.app.log.error(f"处理请求失败 {method} {self.path}\n{traceback.format_exc()}")
            self._error(500, "服务器内部错误")
        finally:
            # 有些接口不读请求体：若连接要复用，未读完的体会让下一个请求错位。
            if not getattr(self, "_body_read", True) and self.headers.get("Content-Length"):
                self.close_connection = True

    # ----------------------------------------------------------------- 响应
    def _hardening(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' blob: data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         "connect-src 'self'; frame-src 'self' blob:; "
                         "object-src 'none'; base-uri 'none'")

    def _send_json(self, obj, status: int = 200, close: bool = False) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self._hardening()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status, close=True)

    def _serve_static(self, path: str) -> bool:
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        entry = STATIC_FILES.get(path)
        if not entry:
            return False
        name, content_type = entry
        target = self.app.web_dir / name
        try:
            body = target.read_bytes()
        except OSError:
            self._error(500, f"界面文件缺失: {name}")
            return True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._hardening()
        self.end_headers()
        self.wfile.write(body)
        return True

    # ----------------------------------------------------------------- 请求辅助
    def _body(self, limit: int = MAX_JSON_BODY) -> dict:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise ApiError(411, "缺少 Content-Length")
        try:
            size = int(raw)
        except ValueError:
            raise ApiError(400, "Content-Length 不合法") from None
        if size < 0 or size > limit:
            raise ApiError(413, "请求体过大")
        data = self.rfile.read(size) if size else b""
        self._body_read = True
        if not data:
            return {}
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "请求体不是合法 JSON") from None
        if not isinstance(value, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return value

    def _token(self) -> str:
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return ""

    def _user(self, required: bool = True) -> dict | None:
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("peer "):
            parts = header[5:].split()
            if len(parts) == 2:
                node_id, token = parts
                peer = self.app.store.get_peer_by_node(node_id)
                if peer and hmac.compare_digest(str(peer["token"]), token):
                    company = self.app.store.get_company()
                    if company and str(peer.get("company_id") or "") != company["company_id"]:
                        raise ApiError(401, "设备不属于当前公司")
                    user = self.app.store.get_user(peer["user_id"])
                    if user and user.get("active"):
                        self.app.store.touch_peer(peer["id"], self.client_address[0])
                        self.peer = peer
                        return user
            if required:
                raise ApiError(401, "设备凭据无效")
            return None

        user = self.app.store.user_for_token(self._token())
        if user is None and required:
            raise ApiError(401, "未登录或登录已过期")
        return user

    def _require_owner(self) -> dict:
        user = self._user()
        if user["role"] != "admin":
            raise ApiError(403, "需要共享方账号")
        return user

    def _require_admin(self) -> dict:
        return self._require_owner()

    def _folder(self, folder_id, need: str = "read") -> tuple[dict, str, dict]:
        user = self._user()
        try:
            fid = int(folder_id)
        except (TypeError, ValueError):
            raise ApiError(400, "文件夹编号不合法") from None
        folder = self.app.store.get_folder(fid)
        if folder is None or folder.get("is_deleted"):
            raise ApiError(404, "共享文件夹不存在")
        level = self.app.store.folder_level(user, folder)
        if level is None:
            raise ApiError(403, "没有该共享文件夹的访问权限")
        if need == "write" and level == "read":
            raise ApiError(403, "只有只读权限，不能修改")
        return folder, level, user

    def _q(self, query: dict, key: str, default: str = "") -> str:
        values = query.get(key)
        return values[0] if values else default

    def _int_q(self, query: dict, key: str, default: int = 0) -> int:
        try:
            return int(self._q(query, key, str(default)) or default)
        except ValueError:
            raise ApiError(400, f"参数 {key} 必须是整数") from None

    # ================================================================= 认证
    def api_health(self, groups, query):
        self._send_json({"ok": True, "app": "ShareBox", "version": __version__,
                         "time": now_iso(), "needs_setup": self.app.needs_setup()})

    def api_company_status(self, groups, query):
        company = self.app.store.get_company()
        token = self.headers.get("X-Company-Token", "")
        self._send_json({
            "exists": company is not None,
            "verified": bool(company is not None and self.app.company_token_valid(token)),
            "name": company["name"] if company else "",
            "company_id": company["company_id"] if company else "",
            "is_creator": bool(company["is_creator"]) if company else False,
            "app": "ShareBox",
            "version": __version__,
        })

    def _bootstrap(self, company_id: str, company_name: str, verifier: str, is_creator: bool,
                   body: dict) -> dict:
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        device_name = str(body.get("device_name", "")).strip() or socket.gethostname()
        try:
            owner = self.app.store.create_user(username, password, "本机用户", "admin")
        except StoreError as exc:
            raise ApiError(400, str(exc)) from None
        self.app.store.create_company(company_id, company_name, verifier,
                                      COMPANY_ITERATIONS, is_creator)
        node_id = uuid.uuid4().hex[:24]
        code = make_recovery_code()
        salt, digest, iterations = hash_password(normalize_code(code), iterations=120_000)
        self.app.store.create_node(node_id, device_name[:64], owner["id"], salt, digest, iterations)
        self.app.refresh_node()
        self.app.company_key = derive_company_key(str(body.get("code", "")), company_id)
        session = self.app.store.create_session(owner["id"], 7, "setup")
        company_token = self.app.issue_company_token()
        self.app.store.audit(owner["id"], "company.create" if is_creator else "company.join",
                             company_name)
        return {
            "token": session["token"],
            "user": _public_user(owner),
            "recovery_code": code,
            "node_id": node_id,
            "device_name": device_name,
            "company_token": company_token,
            "company": {"company_id": company_id, "name": company_name, "is_creator": is_creator},
        }

    def api_company_create(self, groups, query):
        if self.app.store.get_company() is not None:
            raise ApiError(409, "本机已经加入过公司了")
        body = self._body()
        name = str(body.get("company_name", "")).strip()
        code = str(body.get("code", "")).strip()
        if not name or len(name) > 64:
            raise ApiError(400, "公司名称需为 1-64 个字符")
        if len(normalize_code(code)) < 8:
            raise ApiError(400, "公司验证码至少 8 位")
        company_id = make_company_id()
        verifier = company_verifier(code, company_id)
        data = self._bootstrap(company_id, name, verifier, True, body)
        self._send_json(data, 201)

    def api_company_join(self, groups, query):
        if self.app.store.get_company() is not None:
            raise ApiError(409, "本机已经加入过公司了")
        body = self._body()
        company_id = str(body.get("company_id", "")).strip().upper()
        code = str(body.get("code", "")).strip()
        name = str(body.get("company_name", "")).strip() or "已加入的公司"
        if len(company_id) < 8 or "-" not in company_id:
            raise ApiError(400, "公司编号格式不正确，应形如 ABCD-EFGH-IJKL-MNOP")
        if len(normalize_code(code)) < 8:
            raise ApiError(400, "公司验证码至少 8 位")
        verifier = company_verifier(code, company_id)
        data = self._bootstrap(company_id, name, verifier, False, body)
        self._send_json(data, 201)

    def api_company_verify(self, groups, query):
        company = self.app.store.get_company()
        if company is None:
            raise ApiError(409, "本机还没有加入公司")
        if not self.app.pair_allowed("company:" + self.client_address[0]):
            raise ApiError(429, "尝试过于频繁，请稍后再试")
        body = self._body()
        code = str(body.get("code", ""))
        if not hmac.compare_digest(company_verifier(code, company["company_id"]),
                                   company["code_verifier"]):
            raise ApiError(403, "公司验证码不正确")
        self.app.company_key = derive_company_key(code, company["company_id"])
        token = self.app.issue_company_token()
        self._send_json({
            "company_token": token,
            "expires_in": 12 * 3600,
            "company": {"company_id": company["company_id"], "name": company["name"],
                        "is_creator": bool(company["is_creator"])},
        })

    def api_company_info(self, groups, query):
        self._require_owner()
        company = self.app.store.get_company()
        self._send_json({"company": {
            "company_id": company["company_id"],
            "name": company["name"],
            "is_creator": bool(company["is_creator"]),
            "created_at": company["created_at"],
        }})

    def api_recover(self, groups, query):
        if not self.app.pair_allowed("recover:" + self.client_address[0]):
            raise ApiError(429, "尝试过于频繁，请稍后再试")
        node = self.app.store.get_node()
        if node is None:
            raise ApiError(409, "本机还没有初始化")
        body = self._body()
        code = normalize_code(str(body.get("code", "")))
        new_password = str(body.get("new_password", ""))
        if not verify_password(code, node["recovery_salt"], node["recovery_hash"], node["recovery_iter"]):
            raise ApiError(400, "恢复码不正确")
        problem = password_problem(new_password)
        if problem:
            raise ApiError(400, problem)
        self.app.store.set_password(node["owner_id"], new_password)
        self.app.store.audit(node["owner_id"], "account.recover", "使用恢复码重置密码")
        self._send_json({"ok": True})

    # ================================================================= 账号
    def api_account(self, groups, query):
        owner = self._require_owner()
        node = self.app.store.get_node()
        company = self.app.store.get_company()
        self._send_json({
            "username": owner["username"],
            "display_name": owner["display_name"],
            "device_name": node["device_name"],
            "node_id": node["node_id"],
            "created_at": node["created_at"],
            "home": str(Path.home()),
            "company": {
                "company_id": company["company_id"] if company else "",
                "name": company["name"] if company else "",
                "is_creator": bool(company["is_creator"]) if company else False,
            },
        })

    def api_account_password(self, groups, query):
        owner = self._require_owner()
        body = self._body()
        old = str(body.get("old_password", ""))
        new = str(body.get("new_password", ""))
        if not verify_password(old, owner["pw_salt"], owner["pw_hash"], owner["pw_iter"]):
            raise ApiError(400, "原密码不正确")
        problem = password_problem(new)
        if problem:
            raise ApiError(400, problem)
        self.app.store.set_password(owner["id"], new)
        session = self.app.store.create_session(owner["id"], 7, "web")
        self.app.store.audit(owner["id"], "account.password", "")
        self._send_json({"ok": True, "token": session["token"]})

    def api_account_recovery(self, groups, query):
        owner = self._require_owner()
        code = make_recovery_code()
        salt, digest, iterations = hash_password(normalize_code(code), iterations=120_000)
        self.app.store.set_recovery(salt, digest, iterations)
        self.app.store.audit(owner["id"], "account.recovery", "重新生成恢复码")
        self._send_json({"code": code})

    def api_account_device(self, groups, query):
        owner = self._require_owner()
        body = self._body()
        name = str(body.get("device_name", "")).strip()
        if not name or len(name) > 64:
            raise ApiError(400, "设备名称需为 1-64 个字符")
        self.app.store.set_device_name(name)
        self._send_json({"ok": True})

    def api_pick_folder(self, groups, query):
        """在本机弹出原生文件夹选择框（仅限 127.0.0.1 调用）。"""
        self._require_owner()
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            raise ApiError(403, "只能在运行 ShareBox 的这台电脑上选择文件夹")
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--pick-folder"]
        else:
            command = [sys.executable, "-m", "sharebox.picker"]
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=180,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
        except subprocess.TimeoutExpired:
            raise ApiError(408, "选择文件夹超时") from None
        except OSError as exc:
            raise ApiError(500, f"无法打开选择框：{exc}") from None
        path = result.stdout.decode("utf-8", "replace").strip()
        if not path and result.returncode != 0:
            raise ApiError(500, "无法打开文件夹选择窗口")
        self._send_json({"path": path})

    # ================================================================= 配对
    def api_device_code(self, groups, query):
        self._require_owner()
        code, ttl = self.app.pairing.new_code()
        node = self.app.store.get_node()
        self._send_json({
            "code": code,
            "expires_in": ttl,
            "node_id": node["node_id"],
            "device_name": node["device_name"],
            "port": self.app.port,
            "addresses": local_ips(),
        })

    def api_pair_hello(self, groups, query):
        if not self.app.pair_allowed(self.client_address[0]):
            raise ApiError(429, "尝试过于频繁")
        if self.app.needs_setup():
            raise ApiError(409, "对方还没有初始化")
        company = self.app.store.get_company()
        if company is None:
            raise ApiError(409, "对方还没有加入公司")
        if self.app.company_key is None:
            raise ApiError(409, "对方还没有完成公司验证")
        if not self.app.pairing.current():
            raise ApiError(409, "对方还没有生成配对码")
        body = self._body()
        remote_node = str(body.get("node_id", "")).strip()
        if not remote_node:
            raise ApiError(400, "缺少设备标识")
        if str(body.get("company_id", "")).strip().upper() != company["company_id"]:
            raise ApiError(403, "不是同一家公司，无法配对")
        nonce = self.app.pairing.add_nonce()
        node = self.app.store.get_node()
        self._send_json({"nonce": nonce, "node_id": node["node_id"], "name": node["device_name"],
                         "company_id": company["company_id"]})

    def api_pair_confirm(self, groups, query):
        if not self.app.pair_allowed(self.client_address[0]):
            raise ApiError(429, "尝试过于频繁")
        if self.app.needs_setup():
            raise ApiError(409, "对方还没有初始化")
        company = self.app.store.get_company()
        if company is None or self.app.company_key is None:
            raise ApiError(409, "对方还没有完成公司验证")
        code = self.app.pairing.current()
        if not code:
            raise ApiError(409, "配对码已过期，请在对方电脑上重新生成")
        body = self._body()
        remote_node = str(body.get("node_id", "")).strip()
        name = str(body.get("name", "")).strip() or "未命名设备"
        nonce = str(body.get("nonce", ""))
        proof = str(body.get("proof", ""))
        try:
            port = int(body.get("port") or 8787)
        except (TypeError, ValueError):
            port = 8787
        if str(body.get("company_id", "")).strip().upper() != company["company_id"]:
            raise ApiError(403, "不是同一家公司，无法配对")
        if not remote_node or not self.app.pairing.use_nonce(nonce):
            raise ApiError(400, "配对请求已失效，请重试")
        node = self.app.store.get_node()
        key = bind_pair_key(self.app.company_key, derive_pair_key(code, node["node_id"], remote_node))
        if not hmac.compare_digest(pair_proof(key, nonce), proof):
            raise ApiError(403, "配对码不正确，或两家公司的验证码不一致")
        peer = self.app.store.upsert_peer(remote_node, name, self.client_address[0], port,
                                          pair_token(key), company["company_id"])
        self.app.store.audit(node["owner_id"], "peer.add", f"{name} ({self.client_address[0]}:{port})")
        self.app.pairing.clear_code()
        self._send_json({"ok": True, "ack": pair_ack(key, nonce),
                         "node_id": node["node_id"], "name": node["device_name"],
                         "company_id": company["company_id"],
                         "peer": peer_public(peer)})

    def api_device_pair(self, groups, query):
        self._require_owner()
        company = self.app.store.get_company()
        if company is None or self.app.company_key is None:
            raise ApiError(409, "请先完成公司验证，再配对设备")
        body = self._body()
        try:
            host, port = normalize_address(str(body.get("address", "")), body.get("port") or 8787)
        except (ValueError, TypeError) as exc:
            raise ApiError(400, str(exc)) from None
        code = normalize_code(str(body.get("code", "")))
        if len(code) < 6:
            raise ApiError(400, "请输入对方电脑上显示的配对码")
        node = self.app.store.get_node()
        base = f"http://{host}:{port}"
        try:
            hello = post_json(f"{base}/api/pair/hello", {
                "node_id": node["node_id"], "name": node["device_name"], "port": self.app.port,
                "company_id": company["company_id"],
            })
            if str(hello.get("company_id", "")).upper() != company["company_id"]:
                raise ApiError(403, "对方不是同一家公司")
            key = bind_pair_key(self.app.company_key,
                                derive_pair_key(code, node["node_id"], hello["node_id"]))
            nonce = hello["nonce"]
            result = post_json(f"{base}/api/pair/confirm", {
                "node_id": node["node_id"], "name": node["device_name"], "port": self.app.port,
                "company_id": company["company_id"],
                "nonce": nonce, "proof": pair_proof(key, nonce),
            })
        except HubError as exc:
            raise ApiError(502, f"配对失败：{exc.message}") from None
        if not hmac.compare_digest(str(result.get("ack", "")), pair_ack(key, nonce)):
            raise ApiError(502, "对方身份校验失败")
        peer = self.app.store.upsert_peer(hello["node_id"], hello.get("name") or host,
                                          host, port, pair_token(key), company["company_id"])
        self.app.store.audit(node["owner_id"], "peer.add", f"{peer['name']} ({host}:{port})")
        self._send_json({"peer": peer_public(peer)})

    def api_list_peers(self, groups, query):
        self._require_owner()
        now = time.time()
        peers = []
        for peer in self.app.store.list_peers():
            item = peer_public(peer)
            try:
                online = (now - parse_iso(peer["last_seen"]).timestamp()) < 120 if peer.get("last_seen") else False
            except Exception:
                online = False
            item["online"] = online
            peers.append(item)
        self._send_json({"peers": peers})

    def api_delete_peer(self, groups, query):
        self._require_owner()
        peer = self.app.store.get_peer(int(groups[0]))
        if not peer:
            raise ApiError(404, "设备不存在")
        node = self.app.store.get_node()
        self.app.store.audit(node["owner_id"], "peer.delete", peer["name"])
        self.app.store.delete_peer(peer["id"])
        self._send_json({"ok": True})

    def api_remote_folders(self, groups, query):
        self._require_owner()
        peer = self.app.store.get_peer(int(groups[0]))
        if not peer:
            raise ApiError(404, "设备不存在")
        node = self.app.store.get_node()
        client = HubClient(f"http://{peer['address']}:{peer['port']}", peer["token"],
                           peer_node=node["node_id"])
        try:
            folders = client.folders()
        except HubError as exc:
            raise ApiError(502, f"对方暂时不可用：{exc.message}") from None
        self._send_json({"folders": folders, "peer": peer_public(peer)})

    # ================================================================= 镜像
    def api_list_mirrors(self, groups, query):
        self._require_owner()
        status = self.app.mirrors.snapshot() if self.app.mirrors else {}
        items = []
        for mirror in self.app.store.list_mirrors():
            item = {key: mirror[key] for key in
                    ("id", "peer_id", "remote_folder_id", "remote_name", "local_path", "enabled")}
            item["peer_name"] = mirror["peer_name"]
            item["status"] = status.get(int(mirror["id"]), {})
            items.append(item)
        self._send_json({"mirrors": items})

    def api_add_mirror(self, groups, query):
        self._require_owner()
        body = self._body()
        try:
            peer_id = int(body.get("peer_id"))
            remote_folder_id = int(body.get("remote_folder_id"))
        except (TypeError, ValueError):
            raise ApiError(400, "参数不合法") from None
        peer = self.app.store.get_peer(peer_id)
        if not peer:
            raise ApiError(404, "设备不存在")
        raw = str(body.get("local_path", "")).strip()
        if not raw:
            raise ApiError(400, "请填写本地保存目录")
        local = Path(raw).expanduser()
        if not local.is_absolute():
            raise ApiError(400, "请填写完整的本地路径")
        node = self.app.store.get_node()
        client = HubClient(f"http://{peer['address']}:{peer['port']}", peer["token"],
                           peer_node=node["node_id"])
        try:
            folders = {int(item["id"]): item for item in client.folders()}
        except HubError as exc:
            raise ApiError(502, f"对方暂时不可用：{exc.message}") from None
        info = folders.get(remote_folder_id)
        if info is None:
            raise ApiError(403, "对方没有把这个文件夹共享给你")
        try:
            local.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ApiError(400, f"无法创建本地目录：{exc}") from None
        try:
            mirror = self.app.store.add_mirror(peer_id, remote_folder_id,
                                              str(info.get("name", "")), str(local))
        except StoreError as exc:
            raise ApiError(400, str(exc)) from None
        node = self.app.store.get_node()
        self.app.store.audit(node["owner_id"], "mirror.add", f"{peer['name']}/{info.get('name')}")
        self._send_json({"mirror": mirror}, 201)

    def api_delete_mirror(self, groups, query):
        self._require_owner()
        self.app.store.delete_mirror(int(groups[0]))
        self._send_json({"ok": True})

    def api_toggle_mirror(self, groups, query):
        self._require_owner()
        body = self._body()
        mirror = self.app.store.get_mirror(int(groups[0]))
        if not mirror:
            raise ApiError(404, "同步项不存在")
        enabled = bool(body.get("enabled", not mirror["enabled"]))
        self.app.store.set_mirror_enabled(mirror["id"], enabled)
        self._send_json({"ok": True, "enabled": enabled})

    def api_logs(self, groups, query):
        self._require_owner()
        limit = min(500, max(1, self._int_q(query, "limit", 200)))
        self._send_json({"items": self.app.store.list_audit(limit)})

    def api_login(self, groups, query):
        body = self._body()
        company = self.app.store.get_company()
        if company is not None and not self.app.company_token_valid(str(body.get("company_token", ""))):
            raise ApiError(401, "请先完成公司验证")
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        client_ip = self.client_address[0]
        key = f"{client_ip}|{username.lower()}"
        remaining = self.app.rate_remaining(key)
        if remaining > 0:
            raise ApiError(429, f"登录尝试过于频繁，请 {remaining} 秒后再试")
        user = self.app.store.authenticate(username, password)
        if user is None:
            self.app.rate_fail(key)
            raise ApiError(401, "用户名或密码错误")
        self.app.rate_ok(key)
        days = 365 if body.get("client") == "sync" else 1
        session = self.app.store.create_session(user["id"], days, str(body.get("device", "")))
        self.app.store.audit(user["id"], "login", client_ip)
        self._send_json({"token": session["token"], "expires_at": session["expires_at"],
                         "user": _public_user(user)})

    def api_logout(self, groups, query):
        token = self._token()
        if token:
            self.app.store.delete_session(token)
        self._send_json({"ok": True})

    def api_me(self, groups, query):
        user = self._user()
        self._send_json({"user": _public_user(user)})

    def api_change_password(self, groups, query):
        user = self._user()
        body = self._body()
        old = str(body.get("old_password", ""))
        new = str(body.get("new_password", ""))
        if not verify_password(old, user["pw_salt"], user["pw_hash"], user["pw_iter"]):
            raise ApiError(400, "原密码不正确")
        problem = password_problem(new)
        if problem:
            raise ApiError(400, problem)
        self.app.store.set_password(user["id"], new)
        session = self.app.store.create_session(user["id"], 1, "web")
        self.app.store.audit(user["id"], "password.change", "")
        self._send_json({"ok": True, "token": session["token"]})

    # ================================================================= 文件夹
    def api_list_folders(self, groups, query):
        user = self._user()
        folders = self.app.store.list_folders_for(user)
        self._send_json({"folders": folders})

    def api_create_folder(self, groups, query):
        user = self._require_owner()
        body = self._body()
        raw_path = str(body.get("local_path", "")).strip()
        local_path = None
        if raw_path:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                raise ApiError(400, "请填写完整的本地目录路径")
            if not candidate.is_dir():
                raise ApiError(400, "这个目录不存在，或不是文件夹")
            local_path = str(candidate.resolve())
        name = str(body.get("name", "")).strip() or (Path(local_path).name if local_path else "")
        if not name or len(name) > 64:
            raise ApiError(400, "文件夹名称需为 1-64 个字符")
        if any(ch in '<>:"/\\|?*' for ch in name) or name in (".", ".."):
            raise ApiError(400, "文件夹名称包含非法字符")
        try:
            folder = self.app.store.create_folder(name, user["id"], local_path)
        except StoreError as exc:
            raise ApiError(400, str(exc)) from None
        self.app.fs(folder).ensure()
        if local_path and self.app.scanner:
            try:
                self.app.scanner.scan_folder(folder, user["id"])
            except Exception:
                pass
        self.app.store.audit(user["id"], "folder.create", f"{name} -> {local_path or '应用托管'}")
        self._send_json({"folder": {"id": folder["id"], "name": folder["name"],
                                    "local_path": local_path or "", "level": "write"}}, 201)

    def api_get_folder(self, groups, query):
        folder, level, _ = self._folder(groups[0])
        self._send_json({"folder": {
            "id": folder["id"], "name": folder["name"], "owner_id": folder["owner_id"],
            "owner_name": folder["owner_name"], "created_at": folder["created_at"],
            "local_path": folder.get("local_path") or "", "level": level,
        }})

    def api_delete_folder(self, groups, query):
        user = self._require_owner()
        folder, level, _ = self._folder(groups[0], "write")
        if user["role"] != "admin" and folder["owner_id"] != user["id"]:
            raise ApiError(403, "只有共享方可以删除共享")
        self.app.store.delete_folder(folder["id"])
        if not folder.get("local_path"):
            self.app.storage.trash_folder(folder["id"])
        self.app.store.audit(user["id"], "folder.delete", folder["name"])
        self._send_json({"ok": True})

    # ================================================================= 文件
    def api_manifest(self, groups, query):
        folder, level, _ = self._folder(groups[0])
        since = self._int_q(query, "since", 0)
        if since < 0:
            raise ApiError(400, "since 不能为负数")
        data = self.app.store.manifest(folder["id"], since)
        data["folder"] = {"id": folder["id"], "name": folder["name"], "level": level}
        self._send_json(data)

    def api_download(self, groups, query):
        folder, level, _ = self._folder(groups[0])
        rel = normalize_relpath(self._q(query, "path"))
        row = self.app.store.get_file(folder["id"], rel)
        if row is None or row.get("deleted"):
            raise ApiError(404, "文件不存在")
        path = self.app.fs(folder).resolve(rel)
        if not path.is_file():
            raise ApiError(404, "文件已从磁盘丢失")
        size = path.stat().st_size
        name = rel.split("/")[-1]
        ascii_name = safe_filename(name.encode("ascii", "replace").decode("ascii"))
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}")
        self.send_header("Cache-Control", "no-store")
        self._hardening()
        self.end_headers()
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, CHUNK)

    def api_upload(self, groups, query):
        folder, level, user = self._folder(groups[0], "write")
        rel = normalize_relpath(self._q(query, "path"))
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ApiError(411, "缺少 Content-Length")
        try:
            size = int(raw_length)
        except ValueError:
            raise ApiError(400, "Content-Length 不合法") from None
        if size < 0:
            raise ApiError(400, "Content-Length 不合法")
        if size > self.app.max_bytes:
            raise ApiError(413, f"文件超过上限（{self.app.max_bytes // (1024 * 1024)} MB）")

        fs = self.app.fs(folder)
        handle = tempfile.NamedTemporaryFile(prefix="up-", dir=str(fs.tmp_dir()), delete=False)
        tmp_path = Path(handle.name)
        digest = hashlib.sha256()
        received = 0
        try:
            while received < size:
                block = self.rfile.read(min(CHUNK, size - received))
                if not block:
                    raise ApiError(400, "上传中断")
                digest.update(block)
                handle.write(block)
                received += len(block)
            self._body_read = True
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()

            sha = digest.hexdigest()
            existing = self.app.store.get_file(folder["id"], rel)
            if existing and not existing.get("deleted") and existing["sha256"] == sha:
                # 内容完全相同，不制造新的变更（避免多端来回刷）
                tmp_path.unlink(missing_ok=True)
                self._send_json({"seq": self.app.store.current_seq(), "file": {
                    "path": rel, "size": existing["size"], "mtime": existing["mtime"],
                    "sha256": existing["sha256"], "updated_at": existing["updated_at"],
                }, "unchanged": True})
                return

            dest, mtime = fs.install(rel, tmp_path)
            seq = self.app.store.record_file(folder["id"], rel, size, mtime, sha, user["id"])
            self.app.store.audit(user["id"], "file.upload", f"{folder['name']}/{rel}")
            self._send_json({"seq": seq, "file": {
                "path": rel, "size": size, "mtime": mtime, "sha256": sha,
                "updated_at": now_iso(),
            }})
        except BaseException:
            try:
                handle.close()
            except OSError:
                pass
            tmp_path.unlink(missing_ok=True)
            raise

    def api_delete_file(self, groups, query):
        folder, level, user = self._folder(groups[0], "write")
        rel = normalize_relpath(self._q(query, "path"))
        row = self.app.store.get_file(folder["id"], rel)
        moved = self.app.fs(folder).trash(rel)
        seq = self.app.store.current_seq()
        if row is not None and not row.get("deleted"):
            seq = self.app.store.record_delete(folder["id"], rel, user["id"])
            self.app.store.audit(user["id"], "file.delete", f"{folder['name']}/{rel}")
        self._send_json({"ok": True, "seq": seq, "existed": bool(row) or moved})

    def api_mkdir(self, groups, query):
        folder, level, user = self._folder(groups[0], "write")
        body = self._body()
        rel = normalize_relpath(str(body.get("path", "")))
        target = self.app.fs(folder).resolve(rel)
        target.mkdir(parents=True, exist_ok=True)
        if not self.app.store.dir_exists(folder["id"], rel):
            self.app.store.record_dir(folder["id"], rel, False, user["id"])
            self.app.store.audit(user["id"], "dir.create", f"{folder['name']}/{rel}")
        self._send_json({"ok": True})

    def api_delete_dir(self, groups, query):
        folder, level, user = self._folder(groups[0], "write")
        rel = normalize_relpath(self._q(query, "path"))
        if self.app.store.count_live_children(folder["id"], rel) > 0:
            raise ApiError(409, "目录非空，请先删除其中的文件")
        fs = self.app.fs(folder)
        target = fs.resolve(rel)
        if target.is_dir():
            fs.trash(rel)
        self.app.store.record_dir(folder["id"], rel, True, user["id"])
        self.app.store.audit(user["id"], "dir.delete", f"{folder['name']}/{rel}")
        self._send_json({"ok": True})

    # ================================================================= 权限
    def api_get_perms(self, groups, query):
        folder, level, user = self._folder(groups[0])
        if level not in ("admin", "write"):
            raise ApiError(403, "需要管理员或创建者权限")
        self._send_json({
            "folder": {"id": folder["id"], "name": folder["name"], "owner_id": folder["owner_id"]},
            "members": self.app.store.list_perms(folder["id"]),
            "users": [{"id": u["id"], "username": u["username"], "display_name": u["display_name"],
                       "role": u["role"], "active": u["active"]}
                      for u in self.app.store.list_users()],
        })

    def api_put_perm(self, groups, query):
        folder, level, user = self._folder(groups[0])
        if level not in ("admin", "write"):
            raise ApiError(403, "需要管理员或创建者权限")
        body = self._body()
        try:
            target_id = int(body.get("user_id"))
        except (TypeError, ValueError):
            raise ApiError(400, "user_id 不合法") from None
        target = self.app.store.get_user(target_id)
        if target is None:
            raise ApiError(404, "用户不存在")
        if target["id"] == folder["owner_id"]:
            raise ApiError(400, "创建者默认拥有读写权限，无需单独授权")
        new_level = body.get("level")
        if new_level not in (None, "", "none", "read", "write"):
            raise ApiError(400, "权限只能是 read / write / none")
        self.app.store.set_perm(folder["id"], target_id, new_level)
        self.app.store.audit(user["id"], "perm.set", f"{folder['name']}:{target['username']}={new_level or 'none'}")
        self._send_json({"ok": True, "members": self.app.store.list_perms(folder["id"])})

    # ================================================================= 用户
    def api_list_users(self, groups, query):
        user = self._user()
        users = self.app.store.list_users()
        if user["role"] != "admin":
            users = [{"id": u["id"], "username": u["username"], "display_name": u["display_name"],
                      "role": u["role"], "active": u["active"]} for u in users]
        self._send_json({"users": users})

    def api_create_user(self, groups, query):
        admin = self._require_admin()
        body = self._body()
        try:
            user = self.app.store.create_user(
                str(body.get("username", "")),
                str(body.get("password", "")),
                str(body.get("display_name", "")),
                str(body.get("role", "member")),
            )
        except StoreError as exc:
            raise ApiError(400, str(exc)) from None
        self.app.store.audit(admin["id"], "user.create", user["username"])
        self._send_json({"user": _public_user(user)}, 201)

    def api_patch_user(self, groups, query):
        admin = self._require_admin()
        try:
            target_id = int(groups[0])
        except (TypeError, ValueError):
            raise ApiError(400, "用户编号不合法") from None
        target = self.app.store.get_user(target_id)
        if target is None:
            raise ApiError(404, "用户不存在")
        body = self._body()

        wants_role = body.get("role")
        wants_active = body.get("active")
        if (wants_role is not None and wants_role != target["role"]) or \
           (wants_active is not None and bool(wants_active) != bool(target["active"])):
            active_admins = [u for u in self.app.store.list_users()
                             if u["role"] == "admin" and u["active"]]
            losing_admin = target["role"] == "admin" and target["active"] and (
                (wants_role is not None and wants_role != "admin") or
                (wants_active is not None and not wants_active))
            if losing_admin and len(active_admins) <= 1:
                raise ApiError(400, "至少要保留一个启用状态的管理员")

        try:
            if "password" in body and body["password"]:
                self.app.store.set_password(target_id, str(body["password"]))
            if wants_role is not None:
                self.app.store.set_role(target_id, str(wants_role))
            if wants_active is not None:
                self.app.store.set_active(target_id, bool(wants_active))
            if "display_name" in body:
                self.app.store.set_display_name(target_id, str(body["display_name"]))
        except StoreError as exc:
            raise ApiError(400, str(exc)) from None
        self.app.store.audit(admin["id"], "user.update", target["username"])
        self._send_json({"user": _public_user(self.app.store.get_user(target_id))})

    def api_audit_log(self, groups, query):
        self._require_admin()
        limit = min(500, max(1, self._int_q(query, "limit", 200)))
        self._send_json({"items": self.app.store.list_audit(limit)})


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: HubApp):
        self.app = app
        super().__init__(address, Handler)


# --------------------------------------------------------------------------- 启动
def run_hub(args) -> int:
    from .util import Logger

    data_dir = Path(args.data).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    log = Logger("hub", data_dir / "hub.log", verbose=args.verbose)

    app = HubApp(data_dir, log, max_upload_mb=args.max_upload_mb)
    host = args.host
    port = args.port
    app.host = host
    app.port = port

    store = app.store
    store.purge_expired_sessions()
    if store.get_node() is None and args.admin_password and getattr(args, "company_code", ""):
        # 非交互初始化（IT 批量部署用）
        username = args.admin or "admin"
        company_id = make_company_id()
        company_name = getattr(args, "company_name", "") or "我的公司"
        verifier = company_verifier(args.company_code, company_id)
        try:
            owner = store.create_user(username, args.admin_password, "本机用户", "admin")
        except StoreError as exc:
            log.error(f"创建账号失败：{exc}")
            return 2
        store.create_company(company_id, company_name, verifier, COMPANY_ITERATIONS, True)
        node_id = uuid.uuid4().hex[:24]
        code = make_recovery_code()
        salt, digest, iterations = hash_password(normalize_code(code), iterations=120_000)
        store.create_node(node_id, socket.gethostname()[:64], owner["id"], salt, digest, iterations)
        app.refresh_node()
        app.company_key = derive_company_key(args.company_code, company_id)
        log.info(f"已创建公司：{company_name}（编号 {company_id}）")
        log.info(f"已创建本机账号：{username}")
        log.info(f"恢复码（请立即抄下）：{code}")
        log.info(f"恢复码也保存在：{data_dir / '恢复码.txt'}")
        try:
            (data_dir / "恢复码.txt").write_text(
                f"ShareBox 恢复码\n公司：{company_name}\n公司编号：{company_id}\n"
                f"用户名：{username}\n节点：{node_id}\n恢复码：{code}\n"
                f"生成时间：{now_iso()}\n\n忘记密码时，在登录页点「忘记密码」并输入此恢复码。\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    elif store.get_node() is None:
        log.info("首次运行：请用浏览器打开下面的地址，创建本机账号。")

    server = HubServer((host, port), app)

    scheme = "https" if args.tls_cert else "http"
    log.info(f"ShareBox 已启动：{scheme}://127.0.0.1:{port}")
    for ip in local_ips():
        log.info(f"  本机地址：{scheme}://{ip}:{port}")
    if host not in ("0.0.0.0", "::"):
        log.info(f"  当前仅监听 {host}，其他电脑可能无法访问，请用 --host 0.0.0.0")
    log.info(f"数据目录：{data_dir}")
    log.info("按 Ctrl+C 停止。")

    app.scanner = ShareScanner(app)
    app.scanner.start()
    app.mirrors = MirrorService(app)
    app.mirrors.start()

    if args.tls_cert:
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("正在停止……")
    finally:
        if app.scanner:
            app.scanner.stop_event.set()
        if app.mirrors:
            app.mirrors.stop_event.set()
        server.shutdown()
        server.server_close()
        log.close()
    return 0
