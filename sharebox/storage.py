"""共享文件夹的磁盘存储（以 folder_id 分目录，删除进回收站）。"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .paths import resolve_under


class FolderStorage:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "files"
        self.trash_root = self.data_dir / "trash"
        self.root.mkdir(parents=True, exist_ok=True)
        self.trash_root.mkdir(parents=True, exist_ok=True)

    def folder_dir(self, folder_id: int, create: bool = True) -> Path:
        path = self.root / str(int(folder_id))
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve(self, folder_id: int, rel: str) -> Path:
        return resolve_under(self.folder_dir(folder_id), rel)

    def tmp_dir(self, folder_id: int) -> Path:
        path = self.folder_dir(folder_id) / ".sharebox-tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def install(self, folder_id: int, rel: str, tmp_path: Path) -> tuple[Path, float]:
        """把临时文件原子地放到目标位置，返回 (路径, mtime)。"""
        dest = self.resolve(folder_id, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_dir():
            raise IsADirectoryError(f"目标已存在同名文件夹: {rel}")
        os.replace(str(tmp_path), str(dest))
        return dest, time.time()

    def move_to_trash(self, folder_id: int, rel: str) -> bool:
        src = self.resolve(folder_id, rel)
        if not src.exists():
            return False
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest_root = self.trash_root / str(int(folder_id)) / stamp
        dest = dest_root / Path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
        except OSError:
            return False
        # 顺手清理空目录
        try:
            parent = src.parent
            root = self.folder_dir(folder_id, create=False)
            while parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass
        return True

    def trash_folder(self, folder_id: int) -> None:
        src = self.folder_dir(folder_id, create=False)
        if not src.exists():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.trash_root / f"folder-{int(folder_id)}-{stamp}"
        try:
            shutil.move(str(src), str(dest))
        except OSError:
            pass


class ShareFS:
    """绑定某个共享文件夹的文件系统视图。

    共享的可以是用户自己选的本地目录（local_path），也可以是应用托管的目录。
    """

    def __init__(self, base: FolderStorage, folder: dict):
        self.base = base
        self.folder_id = int(folder["id"])
        local = (folder.get("local_path") or "").strip()
        self.root = Path(local).expanduser() if local else base.folder_dir(self.folder_id)

    def ensure(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def resolve(self, rel: str) -> Path:
        self.ensure()
        return resolve_under(self.root, rel)

    def tmp_dir(self) -> Path:
        path = self.ensure() / ".sharebox-tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def install(self, rel: str, tmp_path: Path) -> tuple[Path, float]:
        dest = self.resolve(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_dir():
            raise IsADirectoryError(f"目标已存在同名文件夹: {rel}")
        os.replace(str(tmp_path), str(dest))
        return dest, dest.stat().st_mtime

    def trash(self, rel: str) -> bool:
        src = self.resolve(rel)
        if not src.exists():
            return False
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.base.trash_root / str(self.folder_id) / stamp / Path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
        except OSError:
            return False
        try:
            parent = src.parent
            root = self.root
            while parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass
        return True

    def purge_stale_tmp(self) -> None:
        """清理上次中断留下的临时文件。"""
        try:
            for item in (self.root / ".sharebox-tmp").glob("*"):
                if item.is_file():
                    item.unlink(missing_ok=True)
        except OSError:
            pass
