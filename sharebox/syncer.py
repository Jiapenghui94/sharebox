"""同步客户端：与中心保持文件夹一致。

设计要点
- 拉取：按 /manifest?since=<seq> 增量获取变更；序号回退时自动全量重来。
- 推送：扫描本地目录，内容变化才上传；只读文件夹只拉不推。
- 冲突：两端都改过同一文件时，保留本地改动为「冲突副本」，再落盘服务器版本。
- 安全：所有来自服务器的路径都经过 resolve_under，绝不允许写出本地根目录。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from . import __version__
from .paths import UnsafePath, resolve_under, should_ignore
from .security import sha256_file
from .util import atomic_write_json, ensure_dir, read_json

UA = f"ShareBox/{__version__} (sync)"


class HubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AuthExpired(HubError):
    def __init__(self):
        super().__init__(401, "登录已过期，请重新配置（sb.py sync --setup）")


class HubClient:
    def __init__(self, base_url: str, token: str = "", insecure: bool = False,
                 ca_file: str | None = None, timeout: float = 60.0,
                 peer_node: str = ""):
        parsed = urlsplit((base_url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("中心地址格式不正确，应形如 http://192.168.1.10:8787")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.base_path = parsed.path.rstrip("/")
        self.token = token or ""
        self.peer_node = peer_node or ""
        self.insecure = insecure
        self.ca_file = ca_file
        self.timeout = timeout

    # ------------------------------------------------------------------ 连接
    def _connection(self, timeout: float | None = None):
        timeout = timeout or self.timeout
        if self.scheme == "https":
            context = ssl.create_default_context(cafile=self.ca_file)
            if self.insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=context)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if self.peer_node:
            headers["Authorization"] = f"Peer {self.peer_node} {self.token}"
        elif self.token:
            headers["Authorization"] = "Bearer " + self.token
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        return self.base_path + path

    @staticmethod
    def _decode_error(resp, payload: bytes) -> HubError:
        message = ""
        try:
            message = json.loads(payload.decode("utf-8")).get("error", "")
        except Exception:
            message = payload[:200].decode("utf-8", "replace")
        if resp.status == 401:
            return AuthExpired()
        return HubError(resp.status, message or f"HTTP {resp.status}")

    def request(self, method: str, path: str, body=None, headers: dict | None = None,
                timeout: float | None = None) -> dict:
        conn = self._connection(timeout)
        try:
            conn.request(method, self._url(path), body=body, headers=self._headers(headers))
            resp = conn.getresponse()
            payload = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise HubError(0, f"网络错误：{exc}") from None
        finally:
            conn.close()
        if resp.status >= 400:
            raise self._decode_error(resp, payload)
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HubError(500, "服务器返回了无法解析的内容") from None

    # ------------------------------------------------------------------ 接口
    def login(self, username: str, password: str, device: str = "",
              company_token: str = "") -> dict:
        data = self.request("POST", "/api/login", body=json.dumps({
            "username": username, "password": password, "client": "sync", "device": device,
            "company_token": company_token,
        }).encode("utf-8"), headers={"Content-Type": "application/json"})
        self.token = data["token"]
        return data

    def me(self) -> dict:
        return self.request("GET", "/api/me")

    def folders(self) -> list[dict]:
        return self.request("GET", "/api/folders").get("folders", [])

    def manifest(self, folder_id: int, since: int = 0) -> dict:
        path = f"/api/folders/{folder_id}/manifest"
        if since > 0:
            path += f"?since={since}"
        return self.request("GET", path)

    def upload(self, folder_id: int, rel: str, local_path: Path) -> dict:
        size = local_path.stat().st_size
        timeout = min(3600.0, max(120.0, size / (128 * 1024) + 60))
        url = f"/api/folders/{folder_id}/file?path={quote(rel, safe='')}"
        with open(local_path, "rb") as fh:
            data = self.request("PUT", url, body=fh, headers={
                "Content-Length": str(size),
                "Content-Type": "application/octet-stream",
            }, timeout=timeout)
        return data.get("file", {})

    def download_to(self, folder_id: int, rel: str, dest: Path, expected_sha: str | None = None) -> None:
        timeout = 3600.0
        conn = self._connection(timeout)
        tmp = Path(str(dest) + ".sbpart")
        try:
            conn.request("GET", self._url(
                f"/api/folders/{folder_id}/file?path={quote(rel, safe='')}"), headers=self._headers())
            resp = conn.getresponse()
            if resp.status >= 400:
                payload = resp.read()
                raise self._decode_error(resp, payload)
            ensure_dir(tmp.parent)
            with open(tmp, "wb") as fh:
                while True:
                    block = resp.read(256 * 1024)
                    if not block:
                        break
                    fh.write(block)
        except (OSError, http.client.HTTPException) as exc:
            tmp.unlink(missing_ok=True)
            raise HubError(0, f"下载失败：{exc}") from None
        finally:
            conn.close()
        if expected_sha:
            actual = sha256_file(tmp)
            if actual != expected_sha:
                tmp.unlink(missing_ok=True)
                raise HubError(0, f"下载校验失败：{rel}")
        os.replace(tmp, dest)

    def delete(self, folder_id: int, rel: str) -> dict:
        return self.request("DELETE", f"/api/folders/{folder_id}/file?path={quote(rel, safe='')}")

    def mkdir(self, folder_id: int, rel: str) -> dict:
        return self.request("POST", f"/api/folders/{folder_id}/mkdir",
                            body=json.dumps({"path": rel}).encode("utf-8"),
                            headers={"Content-Type": "application/json"})


# --------------------------------------------------------------------------- 状态
class FolderState:
    def __init__(self, last_seq: int = 0, files: dict | None = None):
        self.last_seq = int(last_seq or 0)
        self.files: dict[str, dict] = files or {}

    @classmethod
    def load(cls, path: Path) -> "FolderState":
        data = read_json(path, default=None) or {}
        return cls(data.get("last_seq", 0), data.get("files", {}))

    def save(self, path: Path) -> None:
        atomic_write_json(path, {"last_seq": self.last_seq, "files": self.files,
                                 "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})


# --------------------------------------------------------------------------- 引擎
class SyncEngine:
    def __init__(self, client: HubClient, folders: dict[str, str], state_dir: Path,
                 log, device: str = "", ignores: list[str] | None = None,
                 poll_seconds: float = 8.0):
        self.client = client
        self.folders = {int(k): Path(v) for k, v in folders.items()}
        self.state_dir = ensure_dir(Path(state_dir))
        self.log = log
        self.device = device or socket.gethostname() or "PC"
        self.ignores = ignores or None
        self.poll_seconds = max(2.0, float(poll_seconds))
        self._hash_cache: dict[tuple, str] = {}
        self.total = {"down": 0, "up": 0, "del_local": 0, "del_remote": 0, "conflict": 0}

    # ------------------------------------------------------------------ 循环
    def run_forever(self) -> None:
        self.log.info(f"开始同步，共 {len(self.folders)} 个文件夹，轮询间隔 {self.poll_seconds:g} 秒")
        while True:
            started = time.time()
            try:
                self.sync_all()
            except AuthExpired:
                self.log.error("登录已过期，请重新运行：python sb.py sync --setup")
                return
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.log.error(f"本轮同步出错：{exc}")
            elapsed = time.time() - started
            time.sleep(max(1.0, self.poll_seconds - elapsed))

    def sync_all(self) -> dict:
        remote = {int(f["id"]): f for f in self.client.folders()}
        for folder_id, local_dir in self.folders.items():
            info = remote.get(folder_id)
            if info is None:
                self.log.warn(f"文件夹 #{folder_id} 已被管理员移除或未授权，跳过")
                continue
            try:
                self.sync_folder(folder_id, local_dir, str(info.get("level", "read")), str(info.get("name", "")))
            except AuthExpired:
                raise
            except Exception as exc:
                self.log.error(f"[{info.get('name')}] 同步失败：{exc}")
        return self.total

    def sync_folder(self, folder_id: int, local_dir: Path, level: str, name: str) -> None:
        local_dir = Path(local_dir)
        if not local_dir.exists():
            self.log.warn(f"[{name}] 本地目录不存在（U 盘未插？），本轮跳过：{local_dir}")
            return
        ensure_dir(local_dir)
        state_path = self.state_dir / f"folder-{folder_id}.json"
        state = FolderState.load(state_path)
        writable = level in ("write", "admin")
        self._hash_cache = {}

        # ---------------------------------------------------------- 1. 拉取
        if state.last_seq > 0:
            resp = self.client.manifest(folder_id, state.last_seq)
            if resp.get("reset") or int(resp.get("seq", 0)) < state.last_seq:
                self.log.warn(f"[{name}] 服务器序号回退，执行一次全量核对")
                resp = self.client.manifest(folder_id, 0)
                state.last_seq = 0
        else:
            resp = self.client.manifest(folder_id, 0)
        self._consume(state, local_dir, resp, folder_id)
        state.save(state_path)

        # ---------------------------------------------------------- 2. 推送
        if writable:
            changes = self._push(state, folder_id, local_dir)
            state.save(state_path)
            if changes:
                # 回读一次，把自己刚产生的变更和并发变更一起消化掉，
                # 这样 next_seq 才能正确推进，避免下一轮重复全量/误判冲突。
                tail = self.client.manifest(folder_id, state.last_seq)
                self._consume(state, local_dir, tail, folder_id)
                state.save(state_path)

    def _consume(self, state: FolderState, local_dir: Path, resp: dict, folder_id: int) -> None:
        """把一次 manifest 响应应用到本地状态和目录。"""
        files_meta = {item["path"]: item for item in resp.get("files", [])}
        if resp.get("full"):
            # 全量模式：以服务器清单为准重建基线，然后重新核对本地
            state.files = {}
        for change in resp.get("changes", []):
            rel = change.get("path", "")
            action = change.get("action")
            if action == "delete":
                self._apply_delete(state, local_dir, rel)
            elif action == "mkdir":
                self._apply_mkdir(local_dir, rel)
            elif action == "rmdir":
                self._apply_rmdir(local_dir, rel)
            else:
                meta = files_meta.get(rel)
                if meta:
                    self._apply_upsert(state, local_dir, meta, folder_id)
        if resp.get("full"):
            for meta in resp.get("files", []):
                self._apply_upsert(state, local_dir, meta, folder_id)
            for rel_dir in resp.get("dirs", []):
                self._apply_mkdir(local_dir, rel_dir)
        seq = int(resp.get("seq", 0))
        state.last_seq = seq if resp.get("full") else max(state.last_seq, seq)

    # ------------------------------------------------------------------ 拉取细节
    def _apply_upsert(self, state: FolderState, root: Path, meta: dict, folder_id: int) -> None:
        rel = meta.get("path", "")
        try:
            dest = resolve_under(root, rel)
        except UnsafePath as exc:
            self.log.warn(f"忽略不安全的远端路径 {rel!r}: {exc}")
            return
        if dest.is_dir():
            self.log.warn(f"本地同名位置是文件夹，跳过：{rel}")
            return
        entry = state.files.get(rel)
        if dest.is_file():
            local_hash = self._hash(dest)
            if local_hash == meta["sha256"]:
                state.files[rel] = self._entry(meta)
                return
            if entry and local_hash == entry.get("sha256"):
                self._download(folder_id, rel, meta, dest)
            else:
                self._rename_conflict(dest, rel)
                state.files.pop(rel, None)
                self._download(folder_id, rel, meta, dest)
                self.total["conflict"] += 1
                self.log.warn(f"检测到两端修改：{rel}，本地改动已另存为冲突副本")
        else:
            self._download(folder_id, rel, meta, dest)
        state.files[rel] = self._entry(meta)

    def _download(self, folder_id: int, rel: str, meta: dict, dest: Path) -> None:
        last_error = None
        for attempt in range(3):
            try:
                self.client.download_to(folder_id, rel, dest, meta.get("sha256"))
                mtime = float(meta.get("mtime") or time.time())
                try:
                    os.utime(dest, (mtime, mtime))
                except OSError:
                    pass
                self.total["down"] += 1
                self.log.info(f"下载 {rel}")
                return
            except PermissionError as exc:  # Windows 上文件被 Office 占用
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
        raise HubError(0, f"写入失败（文件可能被占用）：{rel}：{last_error}")

    def _apply_delete(self, state: FolderState, root: Path, rel: str) -> None:
        try:
            dest = resolve_under(root, rel)
        except UnsafePath:
            state.files.pop(rel, None)
            return
        entry = state.files.get(rel)
        if dest.is_file():
            local_hash = self._hash(dest)
            if entry and local_hash == entry.get("sha256"):
                self._remove(dest)
                self.total["del_local"] += 1
                self.log.info(f"删除 {rel}")
            else:
                self._rename_conflict(dest, rel)
                self.total["conflict"] += 1
                self.log.warn(f"服务器已删除但本地有改动：{rel}，已另存为冲突副本")
        state.files.pop(rel, None)

    def _apply_mkdir(self, root: Path, rel: str) -> None:
        try:
            target = resolve_under(root, rel)
        except UnsafePath:
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log.warn(f"创建目录失败 {rel}：{exc}")

    def _apply_rmdir(self, root: Path, rel: str) -> None:
        try:
            target = resolve_under(root, rel)
        except UnsafePath:
            return
        try:
            target.rmdir()
            self.log.info(f"删除目录 {rel}")
        except OSError:
            # 目录不存在或非空（里面有未同步的文件），保留即可
            pass

    # ------------------------------------------------------------------ 推送细节
    def _push(self, state: FolderState, folder_id: int, root: Path) -> int:
        changes = 0
        local_files, scan_ok = self._scan(root)
        for rel in sorted(local_files):
            path = local_files[rel]
            entry = state.files.get(rel)
            try:
                digest = self._hash(path)
            except OSError as exc:
                self.log.warn(f"读取失败，跳过 {rel}：{exc}")
                continue
            if entry and entry.get("sha256") == digest:
                continue
            try:
                meta = self.client.upload(folder_id, rel, path)
            except AuthExpired:
                raise
            except HubError as exc:
                self.log.warn(f"上传失败 {rel}：{exc.message}")
                continue
            state.files[rel] = self._entry(meta) if meta.get("path") else {
                "path": rel, "size": path.stat().st_size, "mtime": path.stat().st_mtime, "sha256": digest,
            }
            self.total["up"] += 1
            changes += 1
            self.log.info(f"上传 {rel}")

        if not scan_ok:
            self.log.warn("本地目录扫描不完整，本轮不执行「同步删除」以防误删")
            return changes

        pending = [rel for rel in state.files if rel not in local_files]
        # 安全阀：一次删除过多时先停下来，避免目录被误清空（例如网盘未挂载）
        if len(pending) > 10 and len(pending) > len(state.files) * 0.6:
            self.log.error(f"本轮要删除 {len(pending)}/{len(state.files)} 个远端文件，"
                           f"数量异常，已跳过。如确认无误可清理本地状态目录后重试。")
            return changes

        for rel in pending:
            try:
                self.client.delete(folder_id, rel)
            except AuthExpired:
                raise
            except HubError as exc:
                if exc.status == 404:
                    state.files.pop(rel, None)
                else:
                    self.log.warn(f"同步删除失败 {rel}：{exc.message}")
                continue
            state.files.pop(rel, None)
            self.total["del_remote"] += 1
            changes += 1
            self.log.info(f"同步删除 {rel}")
        return changes

    def _scan(self, root: Path) -> tuple[dict[str, Path], bool]:
        found: dict[str, Path] = {}
        errors: list[OSError] = []

        def on_error(exc):  # os.walk 无法进入的子目录
            errors.append(exc)

        for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
            dirnames[:] = [d for d in dirnames if not should_ignore(d, self.ignores)]
            for filename in filenames:
                if should_ignore(filename, self.ignores) or filename.endswith(".sbpart"):
                    continue
                full = Path(dirpath) / filename
                try:
                    if full.is_symlink() or not full.is_file():
                        continue
                    rel = full.relative_to(root).as_posix()
                except (OSError, ValueError):
                    continue
                found[rel] = full
        if errors:
            for exc in errors[:5]:
                self.log.warn(f"扫描受阻：{exc}")
        return found, not errors

    # ------------------------------------------------------------------ 小工具
    def _hash(self, path: Path) -> str:
        try:
            stat = path.stat()
        except OSError:
            raise
        key = (str(path), stat.st_size, int(stat.st_mtime))
        cached = self._hash_cache.get(key)
        if cached:
            return cached
        digest = sha256_file(path)
        self._hash_cache[key] = digest
        return digest

    @staticmethod
    def _entry(meta: dict) -> dict:
        return {
            "path": meta.get("path", ""),
            "size": int(meta.get("size", 0)),
            "mtime": float(meta.get("mtime", 0) or 0),
            "sha256": meta.get("sha256", ""),
        }

    def _rename_conflict(self, dest: Path, rel: str) -> Path:
        pure = PurePosixPath(rel)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        device = "".join(ch for ch in self.device if ch.isalnum() or ch in "-_")[:20] or "PC"
        base = f"{pure.stem} (冲突副本 {device} {stamp})"
        candidate = dest.with_name(base + pure.suffix)
        index = 2
        while candidate.exists():
            candidate = dest.with_name(f"{base} ({index}){pure.suffix}")
            index += 1
        os.replace(dest, candidate)
        return candidate

    def _remove(self, path: Path) -> None:
        for attempt in range(3):
            try:
                path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(0.4 * (attempt + 1))
        path.unlink(missing_ok=True)
