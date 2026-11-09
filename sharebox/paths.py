"""路径安全：所有来自网络的相对路径都必须经过这里。"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path


class UnsafePath(ValueError):
    """非法或越界的路径。"""


_WIN_BAD = set('<>:"|?*')
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# 同步时默认忽略的临时/系统文件（避免 Office 锁文件、缩略图进入共享）
DEFAULT_IGNORE = [
    ".sharebox",
    ".sharebox-tmp",
    "~$*",
    "*.tmp",
    "*.temp",
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
    ".git",
    ".svn",
]


def normalize_relpath(raw: str) -> str:
    """把客户端传来的相对路径规范成 POSIX 形式，并拒绝危险路径。"""
    if raw is None:
        raise UnsafePath("路径为空")
    text = str(raw).replace("\\", "/").strip()
    if not text:
        raise UnsafePath("路径为空")
    if text.startswith("/"):
        raise UnsafePath("不允许绝对路径")
    if _DRIVE_RE.match(text):
        raise UnsafePath("不允许盘符路径")
    if "\x00" in text:
        raise UnsafePath("路径包含非法字符")

    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePath("不允许上级目录")
        if any(ch in _WIN_BAD for ch in part):
            raise UnsafePath(f"路径包含非法字符: {part}")
        if part.endswith(" ") or part.endswith("."):
            # Windows 会静默去掉结尾的空格/点，避免同一文件出现两种写法
            part = part.rstrip(" .")
        if not part:
            continue
        parts.append(part)

    if not parts:
        raise UnsafePath("路径为空")
    rel = "/".join(parts)
    if len(rel) > 900:
        raise UnsafePath("路径过长")
    return rel


def resolve_under(root: Path, rel: str) -> Path:
    """把 rel 解析到 root 之下，并确保没有越界。"""
    rel = normalize_relpath(rel)
    root_abs = Path(root).resolve()
    target = (root_abs / rel).resolve()
    if target != root_abs and root_abs not in target.parents:
        raise UnsafePath("路径越界")
    return target


def should_ignore(name: str, patterns: list[str] | None = None) -> bool:
    patterns = patterns or DEFAULT_IGNORE
    lowered = name.lower()
    for pattern in patterns:
        if fnmatch.fnmatch(lowered, pattern.lower()):
            return True
    return False


def is_safe_folder_name(name: str) -> bool:
    name = (name or "").strip()
    if not name or len(name) > 64:
        return False
    if name in (".", ".."):
        return False
    if any(ch in _WIN_BAD for ch in name):
        return False
    if "/" in name or "\\" in name:
        return False
    return True
