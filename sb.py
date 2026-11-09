#!/usr/bin/env python3
"""ShareBox launcher.

用法:
    python sb.py hub      启动共享中心
    python sb.py sync     启动文件夹同步
    python sb.py doctor   连接自检
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sharebox.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
