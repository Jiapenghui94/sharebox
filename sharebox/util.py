"""通用小工具：时间、JSON、日志、网络信息。"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


# --------------------------------------------------------------------------- 时间
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_after(days: float) -> str:
    return (utcnow() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    value = (value or "").strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_expired(value: str) -> bool:
    try:
        return parse_iso(value) <= utcnow()
    except Exception:
        return True


def human_size(num: float) -> str:
    step = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(step)} B"
            return f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} TB"


# --------------------------------------------------------------------------- 文件
def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def atomic_write_json(path: Path, data) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def default_data_dir() -> Path:
    """客户端默认数据目录（跨平台）。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "ShareBox"
    return Path(os.path.expanduser("~")) / ".sharebox"


# --------------------------------------------------------------------------- 日志
class Logger:
    """控制台 + 可选文件日志，线程安全。"""

    def __init__(self, name: str = "sharebox", log_file: Path | None = None, verbose: bool = False):
        self.name = name
        self.verbose = verbose
        self._lock = threading.Lock()
        self._fh = None
        if log_file:
            ensure_dir(Path(log_file).parent)
            try:
                self._fh = open(log_file, "a", encoding="utf-8")
            except OSError:
                self._fh = None

    def _write(self, level: str, message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} [{level}] {message}"
        with self._lock:
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode("ascii"), flush=True)
            if self._fh:
                try:
                    self._fh.write(line + "\n")
                    self._fh.flush()
                except OSError:
                    pass

    def info(self, message: str) -> None:
        self._write("信息", message)

    def warn(self, message: str) -> None:
        self._write("警告", message)

    def error(self, message: str) -> None:
        self._write("错误", message)

    def debug(self, message: str) -> None:
        if self.verbose:
            self._write("调试", message)

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


# --------------------------------------------------------------------------- 网络
def local_ips() -> list[str]:
    """尽力枚举本机可用于局域网访问的 IPv4。"""
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    # UDP 技巧：不会真的发包，只用来得到出口网卡地址
    for probe in ("8.8.8.8", "223.5.5.5"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 80))
                ip = sock.getsockname()[0]
                if ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except OSError:
            continue
    return ips


def safe_filename(name: str, fallback: str = "文件") -> str:
    name = (name or "").strip()
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = name.strip(" .")
    return name or fallback
