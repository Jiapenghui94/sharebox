"""用 Edge headless 真实渲染对等版界面并截图。

用法：python tools/ui_check.py
输出：.ui-check/*.png
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT = ROOT / ".ui-check"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WebSocket:
    def __init__(self, url: str):
        parts = urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败")
            buffer += chunk

    def _read(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("连接已关闭")
            data += chunk
        return data

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        while True:
            head = self._read(2)
            opcode = head[0] & 0x0F
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            masked = bool(head[1] & 0x80)
            mask = self._read(4) if masked else b""
            payload = self._read(length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x9:
                continue
            if opcode in (0x1, 0x0):
                return payload.decode("utf-8", "replace")


class CDP:
    def __init__(self, ws_url: str):
        self.ws = WebSocket(ws_url)
        self.next_id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        self.next_id += 1
        message_id = self.next_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = json.loads(self.ws.recv())
            if data.get("id") == message_id:
                if "error" in data:
                    raise RuntimeError(f"CDP {method} 失败: {data['error']}")
                return data.get("result", {})
        raise TimeoutError(method)


def wait_for_target(port: int, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                targets = json.loads(response.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("找不到 Edge 调试目标")


def shot(cdp: CDP, name: str) -> Path:
    data = cdp.call("Page.captureScreenshot", {"format": "png"})["data"]
    path = OUT / name
    path.write_bytes(base64.b64decode(data))
    return path


def evaluate(cdp: CDP, expression: str):
    result = cdp.call("Runtime.evaluate", {
        "expression": expression, "returnByValue": True, "awaitPromise": True,
    })
    return result.get("result", {}).get("value")


def wait_for(cdp: CDP, expression: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if evaluate(cdp, expression):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def main() -> int:
    OUT.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sb-ui2-"))
    hub_port = free_port()
    debug_port = free_port()
    url = f"http://127.0.0.1:{hub_port}"
    hub = subprocess.Popen(
        [PY, "sb.py", "hub", "--host", "127.0.0.1", "--port", str(hub_port),
         "--data", str(tmp / "hub")],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    edge = None
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(url + "/api/health", timeout=2)
                break
            except Exception:
                time.sleep(0.2)

        share_dir = tmp / "共享资料"
        share_dir.mkdir(parents=True)
        (share_dir / "项目计划.md").write_text("# 计划\n", encoding="utf-8")
        (share_dir / "预算表.xlsx").write_bytes(b"fake")
        (share_dir / "2026").mkdir()
        (share_dir / "2026" / "一季度.txt").write_text("q1", encoding="utf-8")

        print("[1/5] 启动 Edge 并打开初始化页")
        edge = subprocess.Popen([
            EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--hide-scrollbars",
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={tmp / 'edge'}",
            "--window-size=1360,900", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cdp = CDP(wait_for_target(debug_port))
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": """
            window.__errors = [];
            window.addEventListener('error', e => window.__errors.push(String(e.message || e.error)));
            window.addEventListener('unhandledrejection', e => {
              const r = e.reason; window.__errors.push('promise: ' + String((r && r.message) || r));
            });
        """})
        cdp.call("Page.navigate", {"url": url + "/"})

        assert wait_for(cdp, "document.querySelector('#company-create-form') && !document.querySelector('#company-create-form').hidden")
        time.sleep(0.4)
        shot(cdp, "peer-setup.png")

        print("[2/6] 创建公司并保存恢复码")
        evaluate(cdp, """
            document.querySelector('#cc-name').value = '杭州示例科技有限公司';
            document.querySelector('#cc-code').value = 'Company123';
            document.querySelector('#cc-code2').value = 'Company123';
            document.querySelector('#cc-user').value = 'owner';
            document.querySelector('#cc-pass').value = 'Owner1234';
            document.querySelector('#cc-device').value = '张三的笔记本';
            document.querySelector('#company-create-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
        """)
        ok_recovery = wait_for(cdp, "document.querySelector('.recovery-code') && document.querySelector('.recovery-code').textContent.length > 10")
        code = evaluate(cdp, "document.querySelector('.recovery-code').textContent")
        shot(cdp, "peer-recovery.png")
        evaluate(cdp, "document.querySelector('#modal-foot .btn.primary').click()")
        assert wait_for(cdp, "!document.querySelector('#app-view').hidden")

        print("[3/6] 添加一个本地共享文件夹")
        evaluate(cdp, "document.querySelector('#btn-new-folder').click()")
        assert wait_for(cdp, "!document.querySelector('#modal').hidden && document.querySelectorAll('#modal-body input').length >= 2")
        evaluate(cdp, f"""
            const inputs = document.querySelectorAll('#modal-body input');
            inputs[0].value = '共享资料';
            inputs[1].value = {json.dumps(str(share_dir))};
            document.querySelector('#modal-foot .btn.primary').click();
        """)
        assert wait_for(cdp, "document.querySelectorAll('.folder-item').length >= 1")
        time.sleep(1.0)
        shot(cdp, "peer-files.png")
        rows = evaluate(cdp, "document.querySelectorAll('#file-body tr').length")

        print("[4/6] 设备页 / 日志页")
        evaluate(cdp, "document.querySelector('[data-view=\"devices\"]').click()")
        wait_for(cdp, "document.querySelector('.page-head') !== null")
        time.sleep(0.5)
        shot(cdp, "peer-devices.png")
        evaluate(cdp, "document.querySelector('[data-view=\"logs\"]').click()")
        time.sleep(0.8)
        shot(cdp, "peer-logs.png")

        print("[5/6] 退出后重新验证公司，再登录个人账号")
        evaluate(cdp, "document.querySelector('#btn-logout').click()")
        assert wait_for(cdp, "document.querySelector('#company-verify-form') && !document.querySelector('#company-verify-form').hidden")
        time.sleep(0.4)
        shot(cdp, "peer-verify.png")
        evaluate(cdp, """
            document.querySelector('#verify-code').value = 'Company123';
            document.querySelector('#company-verify-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
        """)
        assert wait_for(cdp, "document.querySelector('#login-form') && !document.querySelector('#login-form').hidden")
        evaluate(cdp, """
            document.querySelector('#login-user').value = 'owner';
            document.querySelector('#login-pass').value = 'Owner1234';
            document.querySelector('#login-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
        """)
        assert wait_for(cdp, "!document.querySelector('#app-view').hidden")

        print("[6/6] 检查前端错误")
        errors = evaluate(cdp, "window.__errors") or []
        print(f"恢复码：{code}；文件行数：{rows}；前端错误：{errors if errors else '无'}")
        if not ok_recovery or errors:
            print("界面检查失败")
            return 1
        return 0
    finally:
        if edge:
            edge.terminate()
            try:
                edge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                edge.kill()
        hub.terminate()
        try:
            hub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hub.kill()


if __name__ == "__main__":
    sys.exit(main())
