"""测试辅助：临时数据目录 + 真实 HTTP 服务。"""
from __future__ import annotations

import gc
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

from sharebox.hub import HubApp, HubServer
from sharebox.peer import company_verifier
from sharebox.store import Store
from sharebox.syncer import HubClient


class NullLog:
    def info(self, *_args, **_kwargs):
        pass

    def warn(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass

    def close(self):
        pass


class HubFixture:
    """在随机端口启动一个真实中心服务。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sb-test-"))
        self.data = self.tmp / "data"
        self.store = Store(self.data)
        self.store.create_user("admin", "Admin1234", "管理员", "admin")
        self.company_id = "TEST-0000-0000-0001"
        self.company_code = "Company123"
        self.store.create_company(self.company_id, "测试公司",
                                  company_verifier(self.company_code, self.company_id), 150_000, True)
        self.app = HubApp(self.data, NullLog())
        self.server = HubServer(("127.0.0.1", 0), self.app)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.company_token = self._verify_company()

    def _verify_company(self) -> str:
        client = HubClient(self.url)
        data = client.request("POST", "/api/company/verify", body=json.dumps({
            "code": self.company_code,
        }).encode(), headers={"Content-Type": "application/json"})
        return data["company_token"]

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        finally:
            for store in (self.store, self.app.store):
                try:
                    store.db.close()
                except Exception:
                    pass
            time.sleep(0.1)
            gc.collect()
            # Windows 上 sqlite 句柄释放有延迟，删不掉也不影响测试结论
            shutil.rmtree(self.tmp, ignore_errors=True)
