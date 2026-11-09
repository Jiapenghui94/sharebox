"""命令行入口：hub / sync / doctor / useradd / passwd。"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from . import __version__
from .security import password_problem
from .syncer import AuthExpired, HubClient, HubError, SyncEngine
from .util import Logger, atomic_write_json, default_data_dir, read_json, safe_filename


def _fix_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sb.py",
        description=f"ShareBox {__version__} - 小团队私有文件共享与同步",
    )
    parser.add_argument("--version", action="version", version=f"ShareBox {__version__}")
    sub = parser.add_subparsers(dest="command")

    hub = sub.add_parser("hub", help="启动共享中心（选一台常开的电脑运行）")
    hub.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    hub.add_argument("--port", type=int, default=8787, help="监听端口，默认 8787")
    hub.add_argument("--data", default="sharebox-data", help="数据目录，默认 ./sharebox-data")
    hub.add_argument("--admin", default="", help="首次启动时创建的管理员用户名")
    hub.add_argument("--admin-password", default="", help="首次启动时创建的管理员密码（非交互）")
    hub.add_argument("--company-name", default="", help="非交互初始化时的公司名称")
    hub.add_argument("--company-code", default="", help="非交互初始化时的公司验证码（至少 8 位）")
    hub.add_argument("--max-upload-mb", type=int, default=2048, help="单文件上限（MB），默认 2048")
    hub.add_argument("--tls-cert", default="", help="可选的 HTTPS 证书文件")
    hub.add_argument("--tls-key", default="", help="可选的 HTTPS 私钥文件")
    hub.add_argument("--verbose", action="store_true")

    sync = sub.add_parser("sync", help="启动文件夹同步（每位同事各运行一份）")
    sync.add_argument("--data", default="", help="客户端数据目录，默认 %%LOCALAPPDATA%%\\ShareBox")
    sync.add_argument("--hub", default="", help="中心地址，如 http://192.168.1.10:8787")
    sync.add_argument("--user", default="", help="登录用户名（配置阶段使用）")
    sync.add_argument("--password", default="", help="登录密码（配置阶段使用，建议交互输入）")
    sync.add_argument("--folder", action="append", default=[],
                      help="映射文件夹，格式 编号=本地路径，可重复；也支持 名称=本地路径")
    sync.add_argument("--setup", action="store_true", help="重新配置（登录、选择文件夹）")
    sync.add_argument("--once", action="store_true", help="只同步一轮就退出")
    sync.add_argument("--poll", type=float, default=0, help="轮询间隔秒数，默认 8")
    sync.add_argument("--install-startup", action="store_true", help="把同步加入 Windows 开机启动")
    sync.add_argument("--verbose", action="store_true")

    doctor = sub.add_parser("doctor", help="检查连接与本地目录状态")
    doctor.add_argument("--data", default="")

    useradd = sub.add_parser("useradd", help="在中心服务器上创建账号")
    useradd.add_argument("username")
    useradd.add_argument("--data", default="sharebox-data")
    useradd.add_argument("--admin", action="store_true", help="创建为管理员")
    useradd.add_argument("--password", default="")

    passwd = sub.add_parser("passwd", help="重置中心服务器上某个账号的密码")
    passwd.add_argument("username")
    passwd.add_argument("--data", default="sharebox-data")
    passwd.add_argument("--password", default="")

    return parser


# --------------------------------------------------------------------------- 工具
def _prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            value = input(f"{text}{suffix}: ").strip()
        except EOFError:
            raise SystemExit("已取消") from None
        if value:
            return value
        if default:
            return default


def _prompt_password(label: str, confirm: bool = True) -> str:
    import getpass
    while True:
        try:
            value = getpass.getpass(f"{label}: ")
            if confirm:
                again = getpass.getpass("再输入一次: ")
                if value != again:
                    print("两次输入不一致，请重试。")
                    continue
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("已取消") from None
        problem = password_problem(value)
        if problem:
            print(problem)
            continue
        return value


def _client_data_dir(args) -> Path:
    if args.data:
        return Path(args.data).expanduser().resolve()
    return default_data_dir()


# --------------------------------------------------------------------------- hub
def cmd_hub(args) -> int:
    if bool(args.tls_cert) != bool(args.tls_key):
        print("--tls-cert 与 --tls-key 必须同时提供")
        return 2
    from .hub import run_hub
    return run_hub(args)


# --------------------------------------------------------------------------- sync
def cmd_sync(args) -> int:
    data_dir = _client_data_dir(args)
    data_dir.mkdir(parents=True, exist_ok=True)
    log = Logger("sync", data_dir / "sync.log", verbose=args.verbose)
    cfg_path = data_dir / "client.json"
    config = read_json(cfg_path, default=None)

    if args.install_startup:
        _install_startup()
        return 0

    need_setup = args.setup or not config or not config.get("token")
    if need_setup:
        config = _setup_wizard(data_dir, cfg_path, config or {}, args, log)
        if config is None:
            return 1

    try:
        client = HubClient(config["hub_url"], config.get("token", ""))
        folders = {str(k): str(v) for k, v in (config.get("folders") or {}).items()}
        if not folders:
            log.error("还没有配置任何文件夹，请重新运行：python sb.py sync --setup")
            return 1
        engine = SyncEngine(
            client, folders, data_dir / "state", log,
            device=config.get("device") or socket.gethostname(),
            ignores=config.get("ignore"),
            poll_seconds=float(args.poll or config.get("poll_seconds") or 8),
        )
        if args.once:
            engine.sync_all()
            log.info("已完成一轮同步")
            return 0
        engine.run_forever()
    except AuthExpired:
        log.error("登录已过期，请重新运行：python sb.py sync --setup")
        return 3
    except HubError as exc:
        log.error(f"无法连接中心：{exc.message}")
        return 3
    except KeyboardInterrupt:
        log.info("已停止同步")
    finally:
        log.close()
    return 0


def _setup_wizard(data_dir: Path, cfg_path: Path, config: dict, args, log: Logger) -> dict | None:
    print(f"=== ShareBox 同步配置（数据目录：{data_dir}）===")
    hub_url = args.hub or config.get("hub_url") or _prompt("中心地址（如 http://192.168.1.10:8787）")
    if not hub_url.startswith(("http://", "https://")):
        hub_url = "http://" + hub_url
    username = args.user or config.get("username") or _prompt("用户名")
    password = args.password or _prompt_password("密码", confirm=False)

    device = config.get("device") or socket.gethostname()
    client = HubClient(hub_url, "")
    company_token = ""
    try:
        import getpass
        company_code = getpass.getpass("公司验证码: ")
        verified = client.request("POST", "/api/company/verify", body=json.dumps({
            "code": company_code,
        }).encode(), headers={"Content-Type": "application/json"})
        company_token = verified["company_token"]
    except (HubError, EOFError, KeyboardInterrupt) as exc:
        message = getattr(exc, "message", "") or "已取消"
        print(f"公司验证失败：{message}")
        return None
    try:
        login = client.login(username, password, device, company_token)
    except HubError as exc:
        print(f"登录失败：{exc.message}")
        return None
    print(f"登录成功：{login['user'].get('display_name') or username}")

    try:
        remote = client.folders()
    except HubError as exc:
        print(f"读取文件夹失败：{exc.message}")
        return None
    if not remote:
        print("当前账号还没有任何共享文件夹。请让管理员在网页端创建并授权后重试。")
        return None

    print("\n可用共享文件夹：")
    for index, folder in enumerate(remote, start=1):
        level = {"read": "只读", "write": "读写", "admin": "管理员"}.get(folder.get("level"), folder.get("level"))
        print(f"  {index}. #{folder['id']} {folder['name']}  ({level})")

    existing = {str(k): str(v) for k, v in (config.get("folders") or {}).items()}
    selected: dict[str, str] = {}

    if args.folder:
        print("\n使用命令行指定的映射：")
        for spec in args.folder:
            if "=" not in spec:
                print(f"  跳过无法解析的参数：{spec}")
                continue
            key, path = spec.split("=", 1)
            key = key.strip().lstrip("#")
            target = next((f for f in remote if str(f["id"]) == key or f["name"] == key), None)
            if target is None:
                print(f"  找不到文件夹：{key}")
                continue
            selected[str(target["id"])] = str(Path(path).expanduser().resolve())
            print(f"  {target['name']} -> {path}")
    else:
        choice = _prompt("\n选择要同步的编号（逗号分隔，all=全部，回车=全部）", "all").lower()
        if choice in ("all", "全部", "*"):
            chosen = remote
        else:
            wanted = {item.strip() for item in choice.split(",") if item.strip()}
            chosen = [f for i, f in enumerate(remote, start=1)
                      if str(i) in wanted or str(f["id"]) in wanted]
        for folder in chosen:
            default = existing.get(str(folder["id"])) or str(
                Path.home() / "ShareBox" / safe_filename(folder["name"])
            )
            path = _prompt(f"  {folder['name']} 的本地目录", default)
            selected[str(folder["id"])] = str(Path(path).expanduser().resolve())

    if not selected:
        print("没有选择任何文件夹。")
        return None

    for path in selected.values():
        Path(path).mkdir(parents=True, exist_ok=True)

    config = {
        "hub_url": hub_url.rstrip("/"),
        "username": username,
        "token": client.token,
        "token_expires": login.get("expires_at", ""),
        "device": device,
        "poll_seconds": float(config.get("poll_seconds") or 8),
        "folders": selected,
        "ignore": config.get("ignore") or [],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    atomic_write_json(cfg_path, config)
    print(f"\n配置已保存：{cfg_path}")
    for fid, path in selected.items():
        print(f"  文件夹 #{fid} -> {path}")
    print("提示：先运行一次 `python sb.py sync --once` 可以立刻做首轮同步。")
    return config


def _install_startup() -> None:
    if not sys.platform.startswith("win"):
        print("该命令仅支持 Windows；其他系统请用 systemd/cron 托管。")
        return
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
    if not startup.is_dir():
        print(f"找不到启动目录：{startup}")
        return
    target = startup / "ShareBox 同步.cmd"
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        target.write_text(
            "@echo off\r\nchcp 65001 >nul\r\n"
            f'cd /d "{exe.parent}"\r\n'
            f'"{exe}" sync\r\n',
            encoding="utf-8",
        )
    else:
        root = Path(__file__).resolve().parent.parent
        target.write_text(
            "@echo off\r\nchcp 65001 >nul\r\n"
            f'cd /d "{root}"\r\n'
            f'"{sys.executable}" sb.py sync\r\n',
            encoding="utf-8",
        )
    print(f"已加入开机启动：{target}")
    print("下次登录 Windows 会自动在后台打开同步窗口。")


# --------------------------------------------------------------------------- doctor
def cmd_doctor(args) -> int:
    data_dir = _client_data_dir(args) if args.data else default_data_dir()
    config = read_json(data_dir / "client.json", default=None)
    print(f"ShareBox {__version__}")
    print(f"客户端数据目录：{data_dir}")
    if not config:
        print("尚未配置。请运行：python sb.py sync --setup")
        return 1
    print(f"中心地址：{config.get('hub_url')}")
    print(f"账号：{config.get('username')}  设备：{config.get('device')}")
    client = HubClient(config["hub_url"], config.get("token", ""))
    try:
        health = client.request("GET", "/api/health")
        print(f"中心服务：正常（版本 {health.get('version')}，服务器时间 {health.get('time')}）")
        me = client.me().get("user", {})
        print(f"登录状态：有效（{me.get('display_name') or me.get('username')}，角色 {me.get('role')}）")
        remote = {int(f["id"]): f for f in client.folders()}
    except AuthExpired:
        print("登录状态：已过期，请运行 python sb.py sync --setup")
        return 3
    except HubError as exc:
        print(f"连接失败：{exc.message}")
        return 3

    print("\n本地文件夹：")
    for fid, path in (config.get("folders") or {}).items():
        info = remote.get(int(fid))
        local = Path(path)
        if info is None:
            print(f"  #{fid} {local}  -> 未授权/已删除")
            continue
        if not local.exists():
            print(f"  #{fid} {info['name']} {local}  -> 目录不存在")
            continue
        count = sum(1 for _ in local.rglob("*") if _.is_file())
        state_file = data_dir / "state" / f"folder-{fid}.json"
        state = read_json(state_file, {}) or {}
        print(f"  #{fid} {info['name']} [{info.get('level')}] {local}")
        print(f"       本地文件 {count} 个，同步序号 {state.get('last_seq', 0)}")
    return 0


# --------------------------------------------------------------------------- 账号
def _offline_store(data_dir: str):
    from .store import Store
    path = Path(data_dir).expanduser().resolve()
    if not (path / "sharebox.db").exists():
        print(f"数据目录里没有数据库：{path}")
        return None
    return Store(path)


def cmd_useradd(args) -> int:
    store = _offline_store(args.data)
    if store is None:
        return 1
    password = args.password or _prompt_password("设置密码")
    try:
        user = store.create_user(args.username, password, args.username, "admin" if args.admin else "member")
    except Exception as exc:
        print(f"创建失败：{exc}")
        return 1
    print(f"已创建账号：{user['username']}（{'管理员' if args.admin else '普通成员'}）")
    return 0


def cmd_passwd(args) -> int:
    store = _offline_store(args.data)
    if store is None:
        return 1
    user = store.get_user_by_name(args.username)
    if user is None:
        print("用户不存在")
        return 1
    password = args.password or _prompt_password("设置新密码")
    try:
        store.set_password(user["id"], password)
    except Exception as exc:
        print(f"重置失败：{exc}")
        return 1
    print(f"已重置 {args.username} 的密码（该账号已登录的会话会失效）")
    return 0


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    _fix_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    handlers = {
        "hub": cmd_hub,
        "sync": cmd_sync,
        "doctor": cmd_doctor,
        "useradd": cmd_useradd,
        "passwd": cmd_passwd,
    }
    return handlers[args.command](args)
