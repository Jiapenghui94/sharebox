"""本机原生文件夹选择框。

由网页调用 /api/pick-folder 时，主程序以 `--pick-folder` 启动这个模块，
选择结果通过标准输出返回给服务器。
"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return 1
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="选择要共享的文件夹")
        root.destroy()
    except Exception:
        return 1
    if path:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
