"""数据访问层：账号、会话、共享文件夹、权限、文件索引、变更日志。"""
from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from pathlib import Path

from .db import Database
from .security import hash_password, new_token, password_problem, token_fingerprint, verify_password
from .util import iso_after, now_iso, parse_iso, utcnow

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{1,31}$")


class StoreError(Exception):
    pass


class Conflict(StoreError):
    pass


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.db = Database(self.data_dir / "sharebox.db")

    # ================================================================== 用户
    def create_user(self, username: str, password: str, display_name: str = "",
                    role: str = "member") -> dict:
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise StoreError("用户名需为 2-32 位字母、数字、下划线、点或横线，且以字母数字开头")
        problem = password_problem(password)
        if problem:
            raise StoreError(problem)
        if role not in ("admin", "member"):
            raise StoreError("角色不合法")
        salt, digest, iterations = hash_password(password)
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO users(username, display_name, pw_salt, pw_hash, pw_iter, role, active, created_at)"
                    " VALUES(?,?,?,?,?,?,1,?)",
                    (username, display_name or username, salt, digest, iterations, role, now_iso()),
                )
                user_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        except sqlite3.IntegrityError:
            raise StoreError("用户名已存在") from None
        return self.get_user(user_id)

    def get_user(self, user_id: int) -> dict | None:
        return _row(self.db.conn().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def get_user_by_name(self, username: str) -> dict | None:
        return _row(self.db.conn().execute(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE", (username or "",)
        ).fetchone())

    def list_users(self) -> list[dict]:
        return _rows(self.db.conn().execute(
            "SELECT id, username, display_name, role, active, created_at FROM users ORDER BY id"
        ).fetchall())

    def count_users(self) -> int:
        return self.db.conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def set_password(self, user_id: int, password: str) -> None:
        problem = password_problem(password)
        if problem:
            raise StoreError(problem)
        salt, digest, iterations = hash_password(password)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE users SET pw_salt=?, pw_hash=?, pw_iter=? WHERE id=?",
                (salt, digest, iterations, user_id),
            )
            # 改密后强制该用户所有会话失效（当前会话由调用方重新签发）
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def set_active(self, user_id: int, active: bool) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))
            if not active:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def set_role(self, user_id: int, role: str) -> None:
        if role not in ("admin", "member"):
            raise StoreError("角色不合法")
        with self.db.tx() as conn:
            conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))

    def set_display_name(self, user_id: int, name: str) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (name.strip(), user_id))

    def authenticate(self, username: str, password: str) -> dict | None:
        user = self.get_user_by_name(username)
        if not user or not user["active"]:
            # 仍然做一次哈希，避免通过响应时间判断用户名是否存在
            verify_password(password or "", "AAAAAAAAAAAAAAAAAAAAAA==", "AAAA", 260000)
            return None
        if not verify_password(password or "", user["pw_salt"], user["pw_hash"], user["pw_iter"]):
            return None
        return user

    # ================================================================== 会话
    def create_session(self, user_id: int, days: float, device: str = "") -> dict:
        token = new_token()
        expires = iso_after(days)
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, device, last_seen)"
                " VALUES(?,?,?,?,?,?)",
                (token_fingerprint(token), user_id, now_iso(), expires, (device or "")[:64], now_iso()),
            )
        return {"token": token, "expires_at": expires}

    def user_for_token(self, token: str) -> dict | None:
        if not token:
            return None
        fingerprint = token_fingerprint(token)
        row = self.db.conn().execute(
            "SELECT u.*, s.expires_at AS s_expires, s.last_seen AS s_last_seen FROM sessions s"
            " JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
            (fingerprint,),
        ).fetchone()
        if row is None:
            return None
        user = dict(row)
        if not user["active"]:
            return None
        try:
            if parse_iso(user["s_expires"]) <= utcnow():
                return None
        except Exception:
            return None
        # last_seen 最多每分钟更新一次，减少写放大
        try:
            last = parse_iso(user["s_last_seen"]) if user["s_last_seen"] else None
            if last is None or (utcnow() - last) > timedelta(minutes=1):
                self.db.conn().execute(
                    "UPDATE sessions SET last_seen=? WHERE token_hash=?", (now_iso(), fingerprint)
                )
        except Exception:
            pass
        return user

    def delete_session(self, token: str) -> None:
        self.db.conn().execute("DELETE FROM sessions WHERE token_hash=?", (token_fingerprint(token),))

    def purge_expired_sessions(self) -> None:
        self.db.conn().execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),))

    # ================================================================== 文件夹
    def create_folder(self, name: str, owner_id: int, local_path: str | None = None) -> dict:
        name = (name or "").strip()
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO folders(name, owner_id, created_at, is_deleted, local_path)"
                    " VALUES(?,?,?,0,?)",
                    (name, owner_id, now_iso(), local_path or None),
                )
                folder_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        except sqlite3.IntegrityError:
            raise StoreError("同名共享文件夹已存在") from None
        return self.get_folder(folder_id)

    def get_folder(self, folder_id: int) -> dict | None:
        return _row(self.db.conn().execute(
            "SELECT f.*, u.username AS owner_name, u.display_name AS owner_display"
            " FROM folders f JOIN users u ON u.id=f.owner_id WHERE f.id=?",
            (folder_id,),
        ).fetchone())

    def delete_folder(self, folder_id: int) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE folders SET is_deleted=1 WHERE id=?", (folder_id,))

    def all_folders(self) -> list[dict]:
        return _rows(self.db.conn().execute(
            "SELECT f.*, u.username AS owner_name FROM folders f JOIN users u ON u.id=f.owner_id"
            " WHERE f.is_deleted=0 ORDER BY f.id"
        ).fetchall())

    def perm_map(self, user_id: int) -> dict[int, str]:
        rows = self.db.conn().execute(
            "SELECT folder_id, level FROM perms WHERE user_id=?", (user_id,)
        ).fetchall()
        return {r["folder_id"]: r["level"] for r in rows}

    def folder_level(self, user: dict, folder: dict | None) -> str | None:
        if folder is None or folder.get("is_deleted"):
            return None
        if user["role"] == "admin":
            return "admin"
        if folder["owner_id"] == user["id"]:
            return "write"
        level = self.perm_map(user["id"]).get(folder["id"])
        return level if level in ("read", "write") else None

    def list_folders_for(self, user: dict) -> list[dict]:
        perms = self.perm_map(user["id"])
        out = []
        for folder in self.all_folders():
            if user["role"] == "admin":
                level = "admin"
            elif folder["owner_id"] == user["id"]:
                level = "write"
            else:
                level = perms.get(folder["id"])
            if level in ("read", "write", "admin"):
                out.append({
                    "id": folder["id"],
                    "name": folder["name"],
                    "owner_id": folder["owner_id"],
                    "owner_name": folder["owner_name"],
                    "created_at": folder["created_at"],
                    "level": level,
                    "local_path": folder.get("local_path") or "",
                })
        return out

    # ================================================================== 权限
    def list_perms(self, folder_id: int) -> list[dict]:
        return _rows(self.db.conn().execute(
            "SELECT p.user_id, p.level, u.username, u.display_name FROM perms p"
            " JOIN users u ON u.id=p.user_id WHERE p.folder_id=? ORDER BY u.id",
            (folder_id,),
        ).fetchall())

    def set_perm(self, folder_id: int, user_id: int, level: str | None) -> None:
        with self.db.tx() as conn:
            if level in (None, "", "none"):
                conn.execute("DELETE FROM perms WHERE folder_id=? AND user_id=?", (folder_id, user_id))
            elif level in ("read", "write"):
                conn.execute(
                    "INSERT INTO perms(folder_id, user_id, level) VALUES(?,?,?)"
                    " ON CONFLICT(folder_id, user_id) DO UPDATE SET level=excluded.level",
                    (folder_id, user_id, level),
                )
            else:
                raise StoreError("权限级别不合法")

    # ================================================================== 文件索引
    def get_file(self, folder_id: int, path: str) -> dict | None:
        return _row(self.db.conn().execute(
            "SELECT * FROM files WHERE folder_id=? AND path=?", (folder_id, path)
        ).fetchone())

    def list_files(self, folder_id: int) -> list[dict]:
        return _rows(self.db.conn().execute(
            "SELECT path, size, mtime, sha256, updated_at, updated_by FROM files"
            " WHERE folder_id=? AND deleted=0 ORDER BY path",
            (folder_id,),
        ).fetchall())

    def current_seq(self) -> int:
        row = self.db.conn().execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM changes").fetchone()
        return int(row["seq"])

    def record_file(self, folder_id: int, path: str, size: int, mtime: float,
                    sha256: str, actor_id: int) -> int:
        """登记（或更新）一个文件，返回新的变更序号。"""
        stamp = now_iso()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO files(folder_id, path, size, mtime, sha256, updated_at, updated_by, deleted)"
                " VALUES(?,?,?,?,?,?,?,0)"
                " ON CONFLICT(folder_id, path) DO UPDATE SET"
                " size=excluded.size, mtime=excluded.mtime, sha256=excluded.sha256,"
                " updated_at=excluded.updated_at, updated_by=excluded.updated_by, deleted=0",
                (folder_id, path, size, mtime, sha256, stamp, actor_id),
            )
            conn.execute(
                "INSERT INTO changes(folder_id, path, action, ts, actor_id) VALUES(?,?,?,?,?)",
                (folder_id, path, "upsert", stamp, actor_id),
            )
            seq = conn.execute("SELECT last_insert_rowid() AS seq").fetchone()["seq"]
        return int(seq)

    def record_delete(self, folder_id: int, path: str, actor_id: int) -> int:
        stamp = now_iso()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE files SET deleted=1, updated_at=?, updated_by=? WHERE folder_id=? AND path=?",
                (stamp, actor_id, folder_id, path),
            )
            conn.execute(
                "INSERT INTO changes(folder_id, path, action, ts, actor_id) VALUES(?,?,?,?,?)",
                (folder_id, path, "delete", stamp, actor_id),
            )
            seq = conn.execute("SELECT last_insert_rowid() AS seq").fetchone()["seq"]
        return int(seq)

    # ------------------------------------------------------------------ 目录（仅空目录需要单独记录）
    def list_dirs(self, folder_id: int) -> list[str]:
        rows = self.db.conn().execute(
            "SELECT path FROM dirs WHERE folder_id=? AND deleted=0 ORDER BY path", (folder_id,)
        ).fetchall()
        return [r["path"] for r in rows]

    def dir_exists(self, folder_id: int, path: str) -> bool:
        row = self.db.conn().execute(
            "SELECT deleted FROM dirs WHERE folder_id=? AND path=?", (folder_id, path)
        ).fetchone()
        return row is not None and not row["deleted"]

    def record_dir(self, folder_id: int, path: str, deleted: bool, actor_id: int) -> int:
        stamp = now_iso()
        action = "rmdir" if deleted else "mkdir"
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO dirs(folder_id, path, updated_at, deleted) VALUES(?,?,?,?)"
                " ON CONFLICT(folder_id, path) DO UPDATE SET updated_at=excluded.updated_at, deleted=excluded.deleted",
                (folder_id, path, stamp, 1 if deleted else 0),
            )
            conn.execute(
                "INSERT INTO changes(folder_id, path, action, ts, actor_id) VALUES(?,?,?,?,?)",
                (folder_id, path, action, stamp, actor_id),
            )
            seq = conn.execute("SELECT last_insert_rowid() AS seq").fetchone()["seq"]
        return int(seq)

    def count_live_children(self, folder_id: int, path: str) -> int:
        """统计某目录下仍然存活的文件和子目录数量（用于「目录非空」判断）。"""
        like = path.replace("%", "\\%").replace("_", "\\_") + "/%"
        files = self.db.conn().execute(
            "SELECT COUNT(*) AS n FROM files WHERE folder_id=? AND deleted=0"
            " AND path LIKE ? ESCAPE '\\'", (folder_id, like),
        ).fetchone()["n"]
        dirs = self.db.conn().execute(
            "SELECT COUNT(*) AS n FROM dirs WHERE folder_id=? AND deleted=0"
            " AND path LIKE ? ESCAPE '\\'", (folder_id, like),
        ).fetchone()["n"]
        return int(files) + int(dirs)

    def manifest(self, folder_id: int, since: int = 0) -> dict:
        """返回增量清单。since=0 时返回全量快照。"""
        conn = self.db.conn()
        head = self.current_seq()
        if since <= 0:
            return {"seq": head, "changes": [], "files": self.list_files(folder_id),
                    "dirs": self.list_dirs(folder_id), "full": True}
        if since > head:
            # 服务端数据被重建或回滚，要求客户端全量重来
            return {"seq": head, "changes": [], "files": self.list_files(folder_id),
                    "dirs": self.list_dirs(folder_id), "full": True, "reset": True}

        changes = _rows(conn.execute(
            "SELECT seq, path, action FROM changes WHERE folder_id=? AND seq>? AND seq<=? ORDER BY seq",
            (folder_id, since, head),
        ).fetchall())
        upserts = sorted({c["path"] for c in changes if c["action"] == "upsert"})
        files = []
        for start in range(0, len(upserts), 400):
            block = upserts[start:start + 400]
            placeholders = ",".join("?" for _ in block)
            files.extend(_rows(conn.execute(
                f"SELECT path, size, mtime, sha256, updated_at FROM files"
                f" WHERE folder_id=? AND deleted=0 AND path IN ({placeholders})",
                (folder_id, *block),
            ).fetchall()))
        made_dirs = sorted({c["path"] for c in changes if c["action"] == "mkdir"})
        dirs = [p for p in made_dirs if self.dir_exists(folder_id, p)]
        return {"seq": head, "changes": changes, "files": files, "dirs": dirs, "full": False}

    # ================================================================== 公司（租户）
    def get_company(self) -> dict | None:
        return _row(self.db.conn().execute("SELECT * FROM company WHERE id=1").fetchone())

    def create_company(self, company_id: str, name: str, verifier: str,
                       iterations: int, is_creator: bool = True) -> dict:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO company(id, company_id, name, code_verifier,"
                " code_iter, created_at, is_creator) VALUES(1,?,?,?,?,?,?)",
                (company_id, name[:64] or "未命名公司", verifier, int(iterations),
                 now_iso(), 1 if is_creator else 0),
            )
        return self.get_company()

    # ================================================================== 节点与设备
    def get_node(self) -> dict | None:
        return _row(self.db.conn().execute("SELECT * FROM node WHERE id=1").fetchone())

    def create_node(self, node_id: str, device_name: str, owner_id: int,
                    recovery_salt: str, recovery_hash: str, recovery_iter: int) -> dict:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO node(id, node_id, device_name, owner_id,"
                " recovery_salt, recovery_hash, recovery_iter, created_at)"
                " VALUES(1,?,?,?,?,?,?,?)",
                (node_id, device_name, owner_id, recovery_salt, recovery_hash, recovery_iter, now_iso()),
            )
        return self.get_node()

    def set_recovery(self, salt: str, digest: str, iterations: int) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE node SET recovery_salt=?, recovery_hash=?, recovery_iter=? WHERE id=1",
                         (salt, digest, iterations))

    def set_device_name(self, name: str) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE node SET device_name=? WHERE id=1", (name.strip()[:64],))

    def add_peer(self, node_id: str, name: str, address: str, port: int,
                 token: str, company_id: str = "") -> dict:
        """为一个已配对设备建立本地账号和记录。"""
        username = "device-" + node_id[:12]
        salt, digest, iterations = hash_password(new_token(), iterations=1000)
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO users(username, display_name, pw_salt, pw_hash, pw_iter, role, active, created_at)"
                    " VALUES(?,?,?,?,?,'member',1,?)",
                    (username, name[:64] or username, salt, digest, iterations, now_iso()),
                )
                user_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                conn.execute(
                    "INSERT INTO peers(node_id, company_id, name, address, port, token, user_id, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (node_id, company_id, name[:64] or username, address, int(port), token, user_id, now_iso()),
                )
        except sqlite3.IntegrityError:
            raise StoreError("该设备已经配对过了") from None
        return self.get_peer_by_node(node_id)

    def upsert_peer(self, node_id: str, name: str, address: str, port: int, token: str,
                    company_id: str = "") -> dict:
        existing = self.get_peer_by_node(node_id)
        if existing:
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE peers SET name=?, address=?, port=?, token=?, company_id=? WHERE id=?",
                    (name[:64] or existing["name"], address, int(port), token,
                     company_id or existing.get("company_id", ""), existing["id"]),
                )
            return self.get_peer_by_node(node_id)
        return self.add_peer(node_id, name, address, port, token, company_id)

    def get_peer(self, peer_id: int) -> dict | None:
        return _row(self.db.conn().execute(
            "SELECT p.*, u.display_name AS user_display FROM peers p"
            " JOIN users u ON u.id=p.user_id WHERE p.id=?", (peer_id,)
        ).fetchone())

    def get_peer_by_node(self, node_id: str) -> dict | None:
        return _row(self.db.conn().execute(
            "SELECT p.*, u.display_name AS user_display FROM peers p"
            " JOIN users u ON u.id=p.user_id WHERE p.node_id=?", (node_id,)
        ).fetchone())

    def list_peers(self) -> list[dict]:
        return _rows(self.db.conn().execute(
            "SELECT p.*, u.display_name AS user_display FROM peers p"
            " JOIN users u ON u.id=p.user_id ORDER BY p.id"
        ).fetchall())

    def touch_peer(self, peer_id: int, ip: str) -> None:
        self.db.conn().execute("UPDATE peers SET last_seen=?, last_ip=? WHERE id=?",
                               (now_iso(), ip[:64], int(peer_id)))

    def delete_peer(self, peer_id: int) -> None:
        peer = self.get_peer(peer_id)
        if not peer:
            return
        with self.db.tx() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (peer["user_id"],))

    # ================================================================== 同步镜像
    def add_mirror(self, peer_id: int, remote_folder_id: int, remote_name: str,
                   local_path: str) -> dict:
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO mirrors(peer_id, remote_folder_id, remote_name, local_path, enabled, created_at)"
                    " VALUES(?,?,?,?,1,?)",
                    (int(peer_id), int(remote_folder_id), remote_name[:64], local_path, now_iso()),
                )
                mirror_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        except sqlite3.IntegrityError:
            raise StoreError("这个共享文件夹已经在同步列表中") from None
        return self.get_mirror(mirror_id)

    def get_mirror(self, mirror_id: int) -> dict | None:
        return _row(self.db.conn().execute("SELECT * FROM mirrors WHERE id=?", (mirror_id,)).fetchone())

    def list_mirrors(self) -> list[dict]:
        return _rows(self.db.conn().execute(
            "SELECT m.*, p.name AS peer_name, p.node_id, p.address, p.port, p.token"
            " FROM mirrors m JOIN peers p ON p.id=m.peer_id ORDER BY m.id"
        ).fetchall())

    def set_mirror_enabled(self, mirror_id: int, enabled: bool) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE mirrors SET enabled=? WHERE id=?",
                         (1 if enabled else 0, int(mirror_id)))

    def delete_mirror(self, mirror_id: int) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM mirrors WHERE id=?", (int(mirror_id),))

    def update_file_mtime(self, folder_id: int, path: str, mtime: float) -> None:
        """仅同步 mtime，不产生变更记录（扫描器用来消除重复变更）。"""
        self.db.conn().execute("UPDATE files SET mtime=? WHERE folder_id=? AND path=?",
                               (float(mtime), int(folder_id), path))

    # ================================================================== 审计
    def audit(self, actor_id: int | None, action: str, detail: str = "") -> None:
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO audit(ts, actor_id, action, detail) VALUES(?,?,?,?)",
                    (now_iso(), actor_id, action, detail[:2000]),
                )
        except sqlite3.Error:
            pass

    def list_audit(self, limit: int = 200) -> list[dict]:
        return _rows(self.db.conn().execute(
            "SELECT a.ts, a.action, a.detail, u.username, u.display_name"
            " FROM audit a LEFT JOIN users u ON u.id=a.actor_id"
            " ORDER BY a.id DESC LIMIT ?",
            (int(limit),),
        ).fetchall())
