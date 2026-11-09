"""ShareBox 图形安装向导 / 控制面板（Windows）。

同一个可执行文件有三种形态：
  1. 命令行：ShareBox.exe hub | sync | doctor | useradd | passwd
  2. 安装向导：双击 ShareBox-Setup-*.exe（尚未安装）
  3. 控制面板：双击已安装的 ShareBox.exe

只依赖 Python 标准库（tkinter + PowerShell 建快捷方式）。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

APP_TITLE = "共享夹"
APP_NAME = "ShareBox"
VERSION = "2.1.7"
FONT = "Microsoft YaHei UI"

CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or Path.home())
APPDATA = Path(os.environ.get("APPDATA") or Path.home())
INSTALL_DIR = LOCALAPPDATA / "Programs" / APP_NAME
DATA_DIR = LOCALAPPDATA / APP_NAME
HUB_DATA = DATA_DIR / "hub-data"
START_MENU = APPDATA / "Microsoft/Windows/Start Menu/Programs" / APP_NAME
STARTUP = APPDATA / "Microsoft/Windows/Start Menu/Programs/Startup"
DESKTOP = Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"

if (Path(os.environ.get("USERPROFILE") or Path.home()) / "OneDrive" / "Desktop").is_dir():
    DESKTOP = Path(os.environ.get("USERPROFILE") or Path.home()) / "OneDrive" / "Desktop"

CLI_COMMANDS = {"hub", "sync", "doctor", "useradd", "passwd"}


# --------------------------------------------------------------------------- 环境
def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_exe() -> Path:
    return Path(sys.executable).resolve()


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def installed_here() -> bool:
    exe = current_exe()
    return frozen() and exe.name.lower() == "sharebox.exe" and (exe.parent / "install.json").exists()


def hide_console() -> None:
    if not frozen():
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


# --------------------------------------------------------------------------- 命令行
def run_cli(argv: list[str]) -> int:
    from sharebox.cli import main as cli_main

    try:
        code = int(cli_main(argv) or 0)
    except SystemExit as exc:
        code = int(exc.code or 0)
    except KeyboardInterrupt:
        code = 0
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror in (10048, 10013):
            print("\n启动失败：端口已被占用（可能共享中心已经在运行）。")
            print("可以换一个端口：ShareBox.exe hub --port 8790")
        else:
            print(f"\n启动失败：{exc}")
        code = 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n启动失败：{exc}")
        code = 1

    if frozen() and code != 0:
        try:
            input("\n按回车键关闭窗口……")
        except EOFError:
            pass
    return code


# --------------------------------------------------------------------------- 快捷方式
def _ps_quote(text) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def create_shortcut(lnk: Path, target: Path, args: str, workdir: Path, description: str) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$sc = $ws.CreateShortcut({_ps_quote(lnk)})\n"
        f"$sc.TargetPath = {_ps_quote(target)}\n"
        f"$sc.Arguments = {_ps_quote(args)}\n"
        f"$sc.WorkingDirectory = {_ps_quote(workdir)}\n"
        f"$sc.Description = {_ps_quote(description)}\n"
        f"$sc.IconLocation = {_ps_quote(str(target) + ',0')}\n"
        "$sc.Save()\n"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, timeout=40, creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError("创建快捷方式失败：" + result.stderr.decode("utf-8", "replace")[:300])


def hub_args() -> str:
    return f'hub --data "{HUB_DATA}"'


def write_shortcuts(exe: Path, desktop: bool, startup: bool) -> None:
    create_shortcut(START_MENU / "启动 ShareBox.lnk", exe, hub_args(), exe.parent, "启动 ShareBox 共享节点")
    create_shortcut(START_MENU / "打开网页.lnk", exe, "open", exe.parent, "打开 ShareBox 网页")
    create_shortcut(START_MENU / "卸载 ShareBox.lnk", exe, "--uninstall", exe.parent, "卸载 ShareBox")
    for stale in ("共享中心.lnk", "文件夹同步.lnk", "检查连接.lnk"):
        _unlink(START_MENU / stale)
    if desktop:
        create_shortcut(DESKTOP / "共享夹.lnk", exe, hub_args(), exe.parent, "启动 ShareBox 共享节点")
    else:
        _unlink(DESKTOP / "共享夹.lnk")
    if startup:
        create_shortcut(STARTUP / "ShareBox 同步.lnk", exe, "hub", exe.parent, "开机自动启动 ShareBox")
    else:
        _unlink(STARTUP / "ShareBox 同步.lnk")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- 安装 / 卸载
def install(target: Path, desktop: bool, startup: bool, launch_after: bool,
            shortcuts: bool = True) -> Path:
    if not frozen():
        raise RuntimeError("当前是源码模式，不能安装。请先运行 build-exe.bat 打包成 exe。")
    target = Path(target).expanduser()
    if not target.is_absolute():
        raise RuntimeError("请填写完整的安装路径")
    cleanup_stale(target.parent)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"无法创建安装目录：{exc}") from None

    dest = target / "ShareBox.exe"
    source = current_exe()
    if source != dest:
        if dest.exists():
            try:
                dest.unlink()
            except PermissionError:
                raise RuntimeError("旧版本正在运行，请先关闭所有 ShareBox 窗口再安装") from None
        shutil.copy2(source, dest)

    marker = {
        "app": APP_NAME,
        "version": VERSION,
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "install_dir": str(target),
        "data_dir": str(DATA_DIR),
    }
    (target / "install.json").write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "使用说明.txt").write_text(
        f"ShareBox 共享夹 {VERSION}（对等版）\n"
        f"========================================\n\n"
        f"安装位置：{target}\n"
        f"数据目录：{DATA_DIR}\n"
        f"   - 共享索引、设备、日志：{HUB_DATA}\n\n"
        f"启动方式\n"
        f"  开始菜单 -> ShareBox -> 启动 ShareBox\n"
        f"  然后浏览器会自动打开（也可以点「打开网页」）\n\n"
        f"怎么用\n"
        f"  1. 第一次打开网页，创建本机账号；保存好恢复码文件。\n"
        f"  2. 「我的共享」里添加一个本地文件夹。\n"
        f"  3. 「设备」里生成配对码，让同事在他的电脑上输入你的地址和配对码。\n"
        f"  4. 「我的共享」里给这台设备设置只读/完全控制。\n"
        f"  5. 同事在「同事共享」里把文件夹同步到本机。\n\n"
        f"异地办公\n"
        f"  双方都安装免费的 Tailscale，用 100.x.x.x 地址互相访问。\n\n"
        f"忘记密码\n"
        f"  登录页点「忘记密码」，输入创建账号时保存的恢复码即可重置。\n\n"
        f"卸载\n"
        f"  开始菜单 -> ShareBox -> 卸载 ShareBox\n",
        encoding="utf-8",
    )
    if shortcuts:
        write_shortcuts(dest, desktop, startup)
    if launch_after:
        launch_hub(dest)
    return dest


def launch_hub(exe: Path, browser: bool = True) -> None:
    subprocess.Popen(
        [str(exe), "hub", "--data", str(HUB_DATA)],
        cwd=str(exe.parent), creationflags=CREATE_NEW_CONSOLE, close_fds=True,
    )
    if browser:
        threading.Timer(2.2, lambda: webbrowser.open("http://127.0.0.1:8787")).start()


def launch_sync(exe: Path) -> None:
    subprocess.Popen(
        [str(exe), "sync"],
        cwd=str(exe.parent), creationflags=CREATE_NEW_CONSOLE, close_fds=True,
    )


def schedule_delete(folder: Path) -> None:
    if not folder.exists():
        return
    script = f"Start-Sleep -Seconds 2; Remove-Item -LiteralPath {_ps_quote(folder)} -Recurse -Force -ErrorAction SilentlyContinue"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS, close_fds=True,
    )


def cleanup_stale(parent: Path | None = None) -> None:
    """删除上次卸载时改名、但当时仍被占用的程序目录。"""
    try:
        parent = Path(parent) if parent else current_exe().parent
        for item in parent.glob(f"{APP_NAME}.deleted-*"):
            shutil.rmtree(item, ignore_errors=True)
    except OSError:
        pass


def uninstall(remove_data: bool) -> None:
    exe = current_exe()
    folder = exe.parent
    shutil.rmtree(START_MENU, ignore_errors=True)
    _unlink(DESKTOP / "共享夹.lnk")
    _unlink(STARTUP / "ShareBox 同步.lnk")
    if remove_data:
        shutil.rmtree(DATA_DIR, ignore_errors=True)

    # 正在运行的程序删不掉自己：先改名，让安装目录立刻消失，再安排延迟清理。
    target = folder
    try:
        trash = folder.with_name(f"{APP_NAME}.deleted-{int(time.time())}")
        folder.rename(trash)
        target = trash
    except OSError:
        pass
    schedule_delete(target)


# --------------------------------------------------------------------------- GUI
def _center(window, width: int, height: int) -> None:
    window.update_idletasks()
    x = max(0, (window.winfo_screenwidth() - width) // 2)
    y = max(0, (window.winfo_screenheight() - height) // 3)
    window.geometry(f"{width}x{height}+{x}+{y}")


def _fit(window, width: int, min_height: int) -> None:
    """按内容实际高度调整窗口，兼容 100% / 150% / 200% 缩放。"""
    window.update_idletasks()
    height = max(min_height, window.winfo_reqheight())
    height = min(height, int(window.winfo_screenheight() * 0.92))
    _center(window, width, height)


def _button(parent, text, command, primary=False, width=None):
    import tkinter as tk

    options = dict(
        text=text, command=command,
        bg="#3b5bdb" if primary else "#eef1f7",
        fg="#ffffff" if primary else "#1c2333",
        activebackground="#2f49b0" if primary else "#e2e7f0",
        activeforeground="#ffffff" if primary else "#1c2333",
        relief="flat", borderwidth=0, highlightthickness=0, cursor="hand2",
        font=(FONT, 10, "bold" if primary else "normal"), padx=18, pady=8,
    )
    if width is not None:
        options["width"] = width
    return tk.Button(parent, **options)


def _header(root, subtitle: str) -> None:
    import tkinter as tk

    header = tk.Frame(root, bg="#3b5bdb")
    header.pack(side="top", fill="x")
    tk.Label(header, text="共享夹 ShareBox", bg="#3b5bdb", fg="#ffffff",
             font=(FONT, 19, "bold")).pack(anchor="w", padx=26, pady=(18, 2))
    tk.Label(header, text=subtitle, bg="#3b5bdb", fg="#dbe4ff",
             font=(FONT, 9)).pack(anchor="w", padx=28, pady=(0, 16))


def _clear(root) -> None:
    for child in root.winfo_children():
        child.destroy()


def show_installer(root, selftest: bool = False) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    _clear(root)
    root.title(f"{APP_TITLE} - 安装向导")
    _center(root, 640, 620)
    _header(root, "小团队文件夹共享与自动同步 · 安装向导")

    footer = tk.Frame(root, bg="#f5f6f8", padx=26, pady=14)
    footer.pack(side="bottom", fill="x")

    body = tk.Frame(root, bg="#ffffff", padx=26, pady=18)
    body.pack(side="top", fill="both", expand=True)

    tk.Label(body, text="安装位置", bg="#ffffff", fg="#5b6478",
             font=(FONT, 9)).pack(anchor="w")
    target_var = tk.StringVar(value=str(INSTALL_DIR))
    line = tk.Frame(body, bg="#ffffff")
    line.pack(fill="x", pady=(6, 4))
    entry = tk.Entry(line, textvariable=target_var, font=(FONT, 9),
                     relief="solid", bd=1, highlightthickness=0)
    entry.pack(side="left", fill="x", expand=True, ipady=6)

    def browse():
        chosen = filedialog.askdirectory(initialdir=str(INSTALL_DIR.parent), title="选择安装位置")
        if chosen:
            target_var.set(str(Path(chosen) / "ShareBox"))

    tk.Button(line, text="浏览…", command=browse, relief="flat", bg="#eef1f7", fg="#1c2333",
              font=(FONT, 9), padx=12, pady=5, borderwidth=0, cursor="hand2").pack(side="left", padx=(8, 0))

    tk.Label(body, text=f"数据（共享文件、账号与配置）会保存在：{DATA_DIR}",
             bg="#ffffff", fg="#8a93a6", font=(FONT, 8), wraplength=560, justify="left").pack(anchor="w")

    tk.Frame(body, bg="#ffffff", height=10).pack()
    desktop_var = tk.BooleanVar(value=True)
    startup_var = tk.BooleanVar(value=False)
    launch_var = tk.BooleanVar(value=True)
    for text, var in (
        ("创建桌面快捷方式", desktop_var),
        ("开机时自动启动文件夹同步", startup_var),
        ("安装完成后立即启动共享中心", launch_var),
    ):
        tk.Checkbutton(body, text=text, variable=var, bg="#ffffff", activebackground="#ffffff",
                       anchor="w", font=(FONT, 9), fg="#1c2333",
                       selectcolor="#ffffff").pack(fill="x", pady=1)

    tk.Label(body, text="安装后可从开始菜单打开「共享中心」或「文件夹同步」。",
             bg="#ffffff", fg="#8a93a6", font=(FONT, 8)).pack(anchor="w", pady=(12, 0))

    status = tk.Label(body, text="", bg="#ffffff", fg="#d64545", font=(FONT, 9), wraplength=560,
                      justify="left")
    status.pack(anchor="w", pady=(8, 0))

    def do_install():
        raw = target_var.get().strip()
        if not raw:
            status.config(text="请填写安装位置", fg="#d64545")
            return
        target = Path(raw)
        install_button.config(state="disabled", text="正在安装…")
        status.config(text="正在复制文件并创建快捷方式…", fg="#5b6478")
        root.update_idletasks()
        try:
            dest = install(target, desktop_var.get(), startup_var.get(), launch_var.get())
        except Exception as exc:  # noqa: BLE001
            status.config(text=str(exc), fg="#d64545")
            install_button.config(state="normal", text="立即安装")
            messagebox.showerror("安装失败", str(exc), parent=root)
            return
        show_finished(root, dest)

    skip = _button(footer, "退出", root.destroy)
    skip.pack(side="right")
    install_button = _button(footer, "立即安装", do_install, primary=True)
    install_button.pack(side="right", padx=(0, 10))

    _fit(root, 640, 560)

    if selftest:
        root.after(400, root.destroy)


def show_finished(root, dest: Path) -> None:
    import tkinter as tk

    _clear(root)
    root.title(f"{APP_TITLE} - 安装完成")
    _center(root, 640, 470)
    _header(root, "安装完成")

    footer = tk.Frame(root, bg="#f5f6f8", padx=26, pady=14)
    footer.pack(side="bottom", fill="x")

    body = tk.Frame(root, bg="#ffffff", padx=26, pady=22)
    body.pack(side="top", fill="both", expand=True)

    tk.Label(body, text="✓", bg="#ffffff", fg="#2f9e6e",
             font=(FONT, 34, "bold")).pack(anchor="w")
    tk.Label(body, text="ShareBox 已安装到本机", bg="#ffffff", fg="#1c2333",
             font=(FONT, 14, "bold")).pack(anchor="w", pady=(4, 2))
    tk.Label(body, text=str(dest), bg="#ffffff", fg="#5b6478",
             font=(FONT, 9), wraplength=560, justify="left").pack(anchor="w")

    tk.Label(body, text="\n下一步：\n"
                        "· 点击「启动 ShareBox」，浏览器会自动打开。\n"
                        "· 第一次使用先创建本机账号，并保存好恢复码文件。\n"
                        "· 然后添加要共享的本地文件夹，在「设备」里和同事配对。",
             bg="#ffffff", fg="#5b6478", font=(FONT, 9), justify="left",
             wraplength=570).pack(anchor="w", pady=(10, 0))

    _button(footer, "完成", root.destroy).pack(side="right")
    _button(footer, "打开网页", lambda: (webbrowser.open("http://127.0.0.1:8787"), root.destroy())).pack(side="right", padx=(0, 10))
    _button(footer, "启动 ShareBox", lambda: (launch_hub(dest), root.destroy()), primary=True).pack(side="right", padx=(0, 10))

    _fit(root, 640, 420)


def show_panel(root) -> None:
    import tkinter as tk
    from tkinter import messagebox

    _clear(root)
    exe = current_exe()
    root.title(f"{APP_TITLE} - 控制面板")
    _center(root, 640, 500)
    _header(root, f"{APP_NAME} {VERSION} · 已安装")

    footer = tk.Frame(root, bg="#f5f6f8", padx=26, pady=12)
    footer.pack(side="bottom", fill="x")

    body = tk.Frame(root, bg="#ffffff", padx=26, pady=16)
    body.pack(side="top", fill="both", expand=True)

    running = hub_running()
    tk.Label(body, text="● ShareBox 正在运行" if running else "● ShareBox 未运行",
             bg="#ffffff", fg="#2f9e6e" if running else "#b7791f",
             font=(FONT, 10, "bold")).pack(anchor="w")
    tk.Label(body, text=f"安装位置：{exe.parent}", bg="#ffffff", fg="#8a93a6",
             font=(FONT, 8), wraplength=570, justify="left", anchor="w").pack(anchor="w", pady=(6, 0))
    tk.Label(body, text=f"数据目录：{DATA_DIR}", bg="#ffffff", fg="#8a93a6",
             font=(FONT, 8), wraplength=570, justify="left", anchor="w").pack(anchor="w")

    grid = tk.Frame(body, bg="#ffffff")
    grid.pack(fill="x", pady=(16, 0))

    def cell(row, column, text, command, primary=False):
        button = _button(grid, text, command, primary=primary)
        button.grid(row=row, column=column, sticky="ew", padx=5, pady=5)
        return button

    grid.columnconfigure(0, weight=1)
    grid.columnconfigure(1, weight=1)
    cell(0, 0, "启动 ShareBox", lambda: launch_hub(exe), True)
    cell(0, 1, "打开网页", open_web)
    cell(1, 0, "打开数据目录", lambda: os.startfile(str(DATA_DIR)))
    cell(1, 1, "查看运行日志", lambda: os.startfile(str(HUB_DATA / "hub.log")))
    cell(2, 0, "卸载", lambda: confirm_uninstall(root))

    tk.Label(body, text="提示：关闭 ShareBox 的黑窗口后，同事将无法连接你的共享；本机文件不受影响。",
             bg="#ffffff", fg="#8a93a6", font=(FONT, 8), wraplength=560,
             justify="left").pack(anchor="w", pady=(16, 0))

    _button(footer, "关闭", root.destroy).pack(side="right")

    _fit(root, 640, 460)


def hub_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8787), timeout=0.4):
            return True
    except OSError:
        return False


def open_web() -> None:
    import tkinter as tk
    from tkinter import messagebox

    if not hub_running():
        messagebox.showinfo("ShareBox 未运行", "请先点击「启动 ShareBox」，等窗口出现后再打开网页。")
        return
    webbrowser.open("http://127.0.0.1:8787")


def show_doctor(root, exe: Path) -> None:
    import tkinter as tk

    window = tk.Toplevel(root)
    window.title("连接检查")
    _center(window, 620, 420)
    text = tk.Text(window, font=("Consolas", 9), wrap="word", padx=14, pady=12,
                   relief="flat", bg="#f7f8fb")
    text.pack(fill="both", expand=True)
    text.insert("end", "正在检查……")
    text.config(state="disabled")
    window.update_idletasks()

    def run():
        try:
            result = subprocess.run([str(exe), "doctor"], capture_output=True, timeout=30,
                                    creationflags=CREATE_NO_WINDOW)
            output = (result.stdout + result.stderr).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            output = f"检查失败：{exc}"
        text.config(state="normal")
        text.delete("1.0", "end")
        text.insert("end", output.strip() or "没有输出")
        text.config(state="disabled")

    window.after(100, run)


def confirm_uninstall(root) -> None:
    from tkinter import messagebox

    if not messagebox.askyesno("卸载 ShareBox", "确定要卸载 ShareBox 吗？\n\n程序会被删除；"
                                                 "已同步的文件和共享数据默认保留。", parent=root):
        return
    remove_data = messagebox.askyesno("删除数据？", "是否同时删除本地数据目录？\n\n"
                                                   f"{DATA_DIR}\n\n"
                                                   "其中包含同步客户端配置、日志以及共享中心的数据。"
                                                   "选择「否」会保留，方便以后重装。", parent=root)
    uninstall(remove_data)
    messagebox.showinfo("已卸载", "ShareBox 已卸载，快捷方式和程序目录已移除。")
    root.destroy()


def run_gui(selftest: bool = False) -> int:
    hide_console()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法打开图形界面：{exc}")
        return 1
    root.configure(bg="#ffffff")
    root.resizable(False, False)
    icon = resource_path("assets/sharebox.ico")
    if icon.exists():
        try:
            root.iconbitmap(default=str(icon))
        except Exception:
            pass
    try:
        root.attributes("-topmost", True)
        root.after(600, lambda: root.attributes("-topmost", False))
    except Exception:
        pass

    if installed_here() and not selftest:
        show_panel(root)
    else:
        show_installer(root, selftest=selftest)

    if selftest:
        root.after(600, root.destroy)
        root.mainloop()
        print("GUI selftest OK")
        return 0

    root.mainloop()
    return 0


# --------------------------------------------------------------------------- 静默安装
def arg_value(argv: list[str], name: str, default=None):
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return default


def silent_install(argv: list[str]) -> int:
    target = Path(arg_value(argv, "--target", str(INSTALL_DIR)))
    try:
        dest = install(target,
                       "--no-desktop" not in argv,
                       "--startup" in argv,
                       False,
                       shortcuts="--no-shortcuts" not in argv)
    except Exception as exc:  # noqa: BLE001
        print(f"安装失败：{exc}")
        return 1
    print(f"已安装：{dest}")
    return 0


# --------------------------------------------------------------------------- 入口
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cleanup_stale()
    if argv and argv[0] == "open":
        webbrowser.open("http://127.0.0.1:8787")
        return 0
    if argv and argv[0] == "--pick-folder":
        hide_console()
        from sharebox.picker import main as picker_main
        return picker_main()
    if argv and argv[0] in CLI_COMMANDS:
        return run_cli(argv)
    if "--version" in argv:
        print(f"ShareBox {VERSION}")
        return 0
    if "--install-silent" in argv:
        return silent_install(argv)
    if "--uninstall-silent" in argv:
        uninstall("--remove-data" in argv)
        return 0
    if "--uninstall" in argv:
        return run_gui() if not installed_here() else _gui_uninstall()
    if "--gui-selftest" in argv:
        return run_gui(selftest=True)
    return run_gui()


def _gui_uninstall() -> int:
    import tkinter as tk
    from tkinter import messagebox

    hide_console()
    root = tk.Tk()
    root.withdraw()
    if not messagebox.askyesno("卸载 ShareBox",
                               "确定要卸载 ShareBox 吗？\n\n程序会被删除；已同步的文件和共享数据默认保留。",
                               parent=root):
        root.destroy()
        return 0
    remove_data = messagebox.askyesno(
        "删除数据？",
        "是否同时删除本地数据目录？\n\n"
        f"{DATA_DIR}\n\n"
        "其中包含本机账号、共享索引、设备配对和日志。选择「否」会保留，方便以后重装。",
        parent=root,
    )
    uninstall(remove_data)
    messagebox.showinfo("已卸载", "ShareBox 已卸载，快捷方式和程序目录已移除。", parent=root)
    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
